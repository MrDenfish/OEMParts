"""Search execution and listing persistence.

Executes a single search against the eBay Browse API, normalizes results,
persists listings, links them to the search, and records price history.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.oem_filter import title_matches_oem
from app.core.price_tracker import record_price_and_detect_change
from app.db import queries
from app.db.models import Search
from app.sources.ebay_browse import search_ebay

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Summary of a single search execution."""

    search_id: str
    listings_fetched: int
    listings_new: int
    listings_updated: int
    api_calls_made: int
    errors: int


def run_single_search(db: Session, search: Search) -> SearchResult:
    """Execute one search against the Browse API and persist results.

    Steps:
      1. Build compatibility filter from the search's vehicle
      2. Call Browse API
      3. For each returned listing: upsert, link to search, record price
      4. Update search.last_fetched_at
      5. Return summary stats
    """
    vehicle = search.vehicle
    listings_new = 0
    listings_updated = 0
    errors = 0

    # Phase 1: Include vehicle details in query text instead of using
    # compatibility_filter, which requires a specific leaf-level category ID.
    # Proper fitment filtering via compatibility_filter + taxonomy lookup
    # will be added when the taxonomy module is implemented.
    enriched_query = (
        f"{vehicle.year} {vehicle.make} {vehicle.model} {search.query_text}"
    )

    # Call Browse API
    normalized_listings = search_ebay(
        db,
        query=enriched_query,
        max_price=search.max_price,
        condition=search.condition_filter,
    )
    api_calls = 1  # One Browse API call per search

    # OEM-only title filter — applied after fetch since the Browse API
    # has no native title-must-contain-keyword filter.
    if search.oem_only and search.oem_number:
        before = len(normalized_listings)
        normalized_listings = [
            listing
            for listing in normalized_listings
            if title_matches_oem(listing.title, search.oem_number)
        ]
        dropped = before - len(normalized_listings)
        if dropped:
            logger.info(
                "OEM-only filter dropped %d/%d listings for search '%s'",
                dropped,
                before,
                search.query_text,
            )

    # Process each returned listing
    for normalized in normalized_listings:
        try:
            listing, is_new = queries.upsert_listing(
                db,
                ebay_item_id=normalized.ebay_item_id,
                title=normalized.title,
                price=normalized.price,
                currency=normalized.currency,
                item_url=normalized.item_url,
                condition=normalized.condition,
                seller_name=normalized.seller_name,
                seller_feedback_score=normalized.seller_feedback_score,
                seller_feedback_pct=normalized.seller_feedback_pct,
                image_url=normalized.image_url,
                ebay_end_date=normalized.ebay_end_date,
                category_id=normalized.category_id,
            )

            if is_new:
                listings_new += 1
            else:
                listings_updated += 1

            # Link listing to this search
            queries.link_search_to_listing(db, search.id, listing.id)

            # Record price snapshot
            record_price_and_detect_change(db, listing, normalized.price)

        except Exception:
            logger.exception(
                "Error processing listing %s for search %s",
                normalized.ebay_item_id,
                search.id,
            )
            errors += 1

    # Update the search's last_fetched_at timestamp
    queries.update_search_last_fetched(db, search.id)
    db.commit()

    logger.info(
        "Search '%s' complete: %d fetched, %d new, %d updated, %d errors",
        search.query_text,
        len(normalized_listings),
        listings_new,
        listings_updated,
        errors,
    )

    return SearchResult(
        search_id=str(search.id),
        listings_fetched=len(normalized_listings),
        listings_new=listings_new,
        listings_updated=listings_updated,
        api_calls_made=api_calls,
        errors=errors,
    )
