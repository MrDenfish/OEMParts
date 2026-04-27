"""Cross-user query deduplication.

Prevents redundant API calls when a search was fetched recently (within TTL).
With one user in Phase 1, the main value is preventing redundant fetches
if the user manually triggers a cycle shortly after a scheduled one.
"""

import logging
from datetime import timedelta

from app.config import settings
from app.db.models import Search, utcnow

logger = logging.getLogger(__name__)


def should_skip_search(search: Search) -> bool:
    """Check if this search was fetched recently enough to skip.

    Returns True if the search's last_fetched_at is within the TTL window,
    meaning we should skip it to avoid redundant API calls.
    """
    if search.last_fetched_at is None:
        return False  # Never fetched — must fetch

    ttl = timedelta(minutes=settings.fetch_default_ttl_minutes)
    cutoff = utcnow() - ttl

    if search.last_fetched_at > cutoff:
        logger.debug(
            "Skipping search %s (last fetched %s, within %d-min TTL)",
            search.id,
            search.last_fetched_at.isoformat(),
            settings.fetch_default_ttl_minutes,
        )
        return True

    return False
