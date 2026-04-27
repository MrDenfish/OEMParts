"""Price history tracking and delta detection.

Records price snapshots on every fetch and detects price changes.
Phase 1: logs changes. Phase 3: will trigger alerts.
"""

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import Listing

logger = logging.getLogger(__name__)


def record_price_and_detect_change(
    db: Session,
    listing: Listing,
    current_price: Decimal,
) -> bool:
    """Record a price snapshot. Returns True if the price changed.

    Always records a new PriceHistory row. Compares with the most recent
    previous snapshot to detect changes.
    """
    # Get most recent price history entry
    history = queries.get_price_history_for_listing(db, listing.id)

    previous_price: Decimal | None = None
    if history:
        previous_price = history[0].price  # Most recent first

    # Record the new snapshot
    queries.record_price_snapshot(db, listing.id, current_price)

    # Detect change
    if previous_price is not None and previous_price != current_price:
        logger.info(
            "Price change for %s (%s): %s -> %s",
            listing.ebay_item_id,
            listing.title[:50],
            previous_price,
            current_price,
        )
        return True

    return False
