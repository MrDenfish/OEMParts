"""Expired listing purge and archival.

Marks stale listings as inactive and archives (deletes) old inactive listings
per configured thresholds.
"""

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.db import queries

logger = logging.getLogger(__name__)


def run_cleanup(db: Session) -> None:
    """Mark stale listings inactive and archive old listings."""
    inactive_count = queries.mark_stale_listings_inactive(
        db, settings.listing_inactive_after_missing_cycles
    )
    logger.info("Marked %d listings as inactive", inactive_count)

    archived_count = queries.archive_old_listings(
        db, settings.listing_archive_after_days
    )
    logger.info("Archived (deleted) %d old listings", archived_count)

    db.commit()
