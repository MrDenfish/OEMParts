"""Fetch cycle orchestrator.

Iterates over active searches, checks deduplication, calls the search
runner for each, and records the cycle in fetch_runs.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.deduplicator import should_skip_search
from app.core.search_runner import run_single_search
from app.db import queries
from app.db.models import utcnow
from app.sources.ebay_item import fetch_item_aspects

logger = logging.getLogger(__name__)

ASPECT_CAP_PER_CYCLE = 200


def enrich_listing_aspects(db: Session, cap: int = ASPECT_CAP_PER_CYCLE) -> int:
    """Fetch Item Specifics for listings that never got them. Returns count.

    One getItem call per listing, once ever (transient failures retry on a
    later cycle). Worker-only by design — request handlers never call this.
    """
    pending = queries.get_listings_needing_aspects(db, cap)
    if len(pending) == cap:
        logger.info("Aspect enrichment cap (%d) reached; remainder next cycle", cap)
    enriched = 0
    for listing in pending:
        try:
            aspects = fetch_item_aspects(db, listing.ebay_item_id)
            if aspects is None:
                continue
            listing.brand = aspects.brand
            listing.mpn = aspects.mpn
            listing.oe_part_number = aspects.oe_part_number
            listing.aspects_fetched_at = utcnow()
            db.commit()
            enriched += 1
        except Exception:
            # One poison listing must not starve the oldest-first queue —
            # log and move on to the next listing, same pattern as the
            # per-search guard in run_fetch_cycle below.
            db.rollback()
            logger.exception("Aspect enrichment failed for listing %s", listing.id)
    return enriched


def run_fetch_cycle(
    db: Session,
    cycle_type: str,
    search_id: str | None = None,
) -> None:
    """Execute a full fetch cycle.

    Args:
        db: Database session.
        cycle_type: "nightly" (all active), "intraday" (high-priority), "manual".
        search_id: If provided, only fetch this specific search (for manual/testing).
    """
    fetch_run = queries.create_fetch_run(db, cycle_type)
    db.commit()

    total_fetched = 0
    total_new = 0
    total_updated = 0
    total_api_calls = 0
    total_errors = 0
    searches_processed = 0

    # Determine which searches to process
    if search_id:
        search = queries.get_search_by_id_unscoped(db, uuid.UUID(search_id))
        search_list = [search] if search else []
        if not search:
            logger.error("Search not found: %s", search_id)
    else:
        search_list = queries.get_active_searches(db, cycle_type)

    logger.info(
        "Starting %s fetch cycle: %d searches to process",
        cycle_type,
        len(search_list),
    )

    for search in search_list:
        if search is None:
            continue

        # Check dedup (skip if fetched recently).
        # Manual cycles and explicit search_id requests bypass dedup.
        if cycle_type != "manual" and not search_id:
            if should_skip_search(search):
                continue

        try:
            result = run_single_search(db, search)
            total_fetched += result.listings_fetched
            total_new += result.listings_new
            total_updated += result.listings_updated
            total_api_calls += result.api_calls_made
            total_errors += result.errors
            searches_processed += 1
        except Exception:
            logger.exception("Error processing search %s", search.id)
            total_errors += 1
            # Continue to next search — don't abort the cycle

    try:
        enriched = enrich_listing_aspects(db)
        if enriched:
            logger.info("Enriched %d listings with Item Specifics", enriched)
    except Exception:
        logger.exception("Aspect enrichment failed; completing cycle anyway")

    # Complete the fetch run record
    queries.complete_fetch_run(
        db,
        fetch_run,
        searches_processed=searches_processed,
        listings_fetched=total_fetched,
        listings_new=total_new,
        listings_updated=total_updated,
        api_calls_made=total_api_calls,
        errors=total_errors,
    )
    db.commit()

    logger.info(
        "Fetch cycle '%s' complete: %d searches, %d listings (%d new), %d errors",
        cycle_type,
        searches_processed,
        total_fetched,
        total_new,
        total_errors,
    )
