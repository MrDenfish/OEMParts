"""eBay Browse API client.

Handles item search with compatibility filters, pagination, response
normalization, and API quota logging.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ApiQuotaLog
from app.sources.ebay_oauth import get_ebay_token

logger = logging.getLogger(__name__)

BROWSE_API_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BROWSE_SANDBOX_URL = "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"


@dataclass
class NormalizedListing:
    """Normalized listing data extracted from eBay Browse API response."""

    ebay_item_id: str
    title: str
    price: Decimal
    currency: str
    condition: str | None
    seller_name: str | None
    seller_feedback_score: int | None
    seller_feedback_pct: Decimal | None
    item_url: str
    image_url: str | None
    ebay_end_date: datetime | None
    category_id: str | None


def _get_browse_url() -> str:
    """Return the correct Browse API URL based on environment config."""
    if settings.ebay_env == "sandbox":
        return BROWSE_SANDBOX_URL
    return BROWSE_API_URL


def _parse_decimal(value: str | None) -> Decimal | None:
    """Safely parse a string to Decimal, returning None on failure."""
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO 8601 datetime string from eBay, returning None on failure."""
    if value is None:
        return None
    try:
        # eBay uses ISO 8601 format like "2026-05-01T12:00:00.000Z"
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _normalize_listing(item: dict) -> NormalizedListing | None:
    """Extract and normalize fields from a single Browse API itemSummary.

    Returns None if required fields (item_id, title, price) are missing.
    """
    ebay_item_id = item.get("itemId")
    title = item.get("title")
    price_info = item.get("price", {})
    price_str = price_info.get("value")
    currency = price_info.get("currency", "USD")

    # Required fields — skip this listing if missing
    if not ebay_item_id or not title or price_str is None:
        logger.warning(
            "Skipping listing with missing required fields: %s",
            item.get("itemId", "unknown"),
        )
        return None

    price = _parse_decimal(price_str)
    if price is None:
        logger.warning("Skipping listing with unparseable price: %s", price_str)
        return None

    # Seller info
    seller = item.get("seller", {})
    seller_feedback_pct_str = seller.get("feedbackPercentage")

    # Image
    image = item.get("image", {})
    image_url = image.get("imageUrl")
    if not image_url:
        # Try thumbnailImages as fallback
        thumbnails = item.get("thumbnailImages", [])
        if thumbnails:
            image_url = thumbnails[0].get("imageUrl")

    # Category
    categories = item.get("categories", [])
    category_id = categories[0].get("categoryId") if categories else None

    # Condition
    condition = item.get("condition")

    # Item URL
    item_url = item.get("itemWebUrl", "")

    return NormalizedListing(
        ebay_item_id=ebay_item_id,
        title=title,
        price=price,
        currency=currency,
        condition=condition,
        seller_name=seller.get("username"),
        seller_feedback_score=seller.get("feedbackScore"),
        seller_feedback_pct=_parse_decimal(seller_feedback_pct_str),
        item_url=item_url,
        image_url=image_url,
        ebay_end_date=_parse_datetime(item.get("itemEndDate")),
        category_id=category_id,
    )


def _log_api_call(
    db: Session, status_code: int | None, provider: str = "ebay_browse"
) -> None:
    """Record an API call in the api_quota_log table."""
    log_entry = ApiQuotaLog(
        provider=provider,
        status_code=status_code,
    )
    db.add(log_entry)
    db.flush()


def search_ebay(
    db: Session,
    query: str,
    compatibility_filter: str | None = None,
    max_price: Decimal | None = None,
    condition: str | None = None,
    limit: int | None = None,
) -> list[NormalizedListing]:
    """Search the eBay Browse API and return normalized listings.

    Args:
        db: Database session (for token management and quota logging).
        query: Search query string (e.g., "LR4 coolant crossover pipe").
        compatibility_filter: Vehicle fitment filter (e.g., "Year:2012,Make:Land Rover,Model:LR4").
        max_price: Maximum price filter.
        condition: Condition filter — "New", "Used", or None for all.
        limit: Max results to return (defaults to config value).

    Returns:
        List of NormalizedListing objects. Empty list on API errors.
    """
    # eBay Browse API condition IDs
    condition_ids = {"new": "1000", "used": "3000"}

    effective_limit = limit or settings.fetch_max_listings_per_query
    token = get_ebay_token(db)

    # Build request parameters
    params: dict[str, str | int] = {
        "q": query,
        "limit": effective_limit,
    }
    if compatibility_filter:
        params["compatibility_filter"] = compatibility_filter
        # eBay requires a fitment-supporting category when using compatibility_filter.
        # 6028 = "eBay Motors Parts & Accessories" (top-level fitment category).
        params["category_ids"] = "6028"

    # Build the filter string (combines price and condition)
    filters: list[str] = []
    if max_price is not None:
        filters.append(f"price:[..{max_price}],priceCurrency:USD")
    if condition and condition.lower() in condition_ids:
        filters.append(f"conditionIds:{{{condition_ids[condition.lower()]}}}")
    if filters:
        params["filter"] = ",".join(filters)

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": settings.ebay_marketplace_id,
    }

    status_code = None
    try:
        with httpx.Client() as client:
            response = client.get(
                _get_browse_url(),
                params=params,
                headers=headers,
                timeout=30.0,
            )
            status_code = response.status_code

        _log_api_call(db, status_code)

        if status_code != 200:
            logger.error(
                "eBay Browse API returned %d for query '%s': %s",
                status_code,
                query,
                response.text[:200],
            )
            return []

        data = response.json()
        item_summaries = data.get("itemSummaries", [])

        if not item_summaries:
            logger.info("No results from eBay for query: '%s'", query)
            return []

        results: list[NormalizedListing] = []
        for item in item_summaries:
            normalized = _normalize_listing(item)
            if normalized is not None:
                results.append(normalized)

        logger.info(
            "eBay search for '%s' returned %d items (%d after normalization)",
            query,
            len(item_summaries),
            len(results),
        )
        return results

    except httpx.TimeoutException:
        logger.error("eBay Browse API timeout for query: '%s'", query)
        _log_api_call(db, None)
        return []
    except httpx.HTTPError as exc:
        logger.error("eBay Browse API HTTP error for query '%s': %s", query, exc)
        _log_api_call(db, status_code)
        return []
