"""Search execution and listing persistence.

Executes a single search against the eBay Browse API, normalizes results,
persists listings, links them to the search, and records price history.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import settings
from app.core.compatibility import build_compatibility_filter
from app.core.oem_filter import title_matches_oem
from app.core.price_tracker import record_price_and_detect_change
from app.db import queries
from app.db.models import Search
from app.sources.ebay_browse import FitmentFilterError, search_ebay

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
      1. Fitment mode (search.category_id set): bare query + category_ids +
         compatibility_filter. Fallback mode (NULL, or eBay rejects the
         filter): vehicle year/make/model prepended to the query text.
      2. For each returned listing: upsert, link to search, record price
      3. Update search.last_fetched_at
      4. Return summary stats
    """
    vehicle = search.vehicle
    listings_new = 0
    listings_updated = 0
    errors = 0

    enriched_query = (
        f"{vehicle.year} {vehicle.make} {vehicle.model} {search.query_text}"
    )

    # OEM-only searches fetch deep: their title filter (below) discards most
    # of the page before anything is persisted, so the default 50 starves
    # them (2026-09-14: alternator search kept 6 of 50 while 125 matched).
    # One call at limit=200 costs the same quota as one call at 50. Searches
    # without the filter keep the default — deeper pages are unfiltered noise.
    fetch_limit = (
        settings.fetch_oem_deep_limit if search.oem_only and search.oem_number else None
    )

    fitment_mode = False
    if search.category_id:
        # Fitment mode: eBay guarantees the part fits, so the query stays
        # bare — listings that fit but don't name the vehicle now match.
        try:
            normalized_listings = search_ebay(
                db,
                query=search.query_text,
                compatibility_filter=build_compatibility_filter(
                    vehicle.year, vehicle.make, vehicle.model
                ),
                category_ids=search.category_id,
                max_price=search.max_price,
                condition=search.condition_filter,
                limit=fetch_limit,
            )
            fitment_mode = True
            api_calls = 1
        except FitmentFilterError:
            logger.warning(
                "eBay rejected fitment filter for search %s '%s' (category %s); "
                "retrying without filter",
                search.id,
                search.query_text,
                search.category_id,
            )
            normalized_listings = search_ebay(
                db,
                query=enriched_query,
                max_price=search.max_price,
                condition=search.condition_filter,
                limit=fetch_limit,
            )
            api_calls = 2
    else:
        # Fallback mode (Phase 1 behavior): no resolved category, so the
        # vehicle is folded into the query text instead.
        normalized_listings = search_ebay(
            db,
            query=enriched_query,
            max_price=search.max_price,
            condition=search.condition_filter,
            limit=fetch_limit,
        )
        api_calls = 1

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
                compatibility_checked=fitment_mode,
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
