"""Backfill / refresh eBay fitment categories for active searches.

Run via `./oemparts taxonomy-sync` (only searches missing a category) or
`./oemparts taxonomy-sync --all` (re-resolve every active search, e.g.
after eBay's category tree changes).
"""

import logging

from sqlalchemy.orm import Session

from app.core.compatibility import ResolvedCategory, resolve_fitment_category
from app.db import queries
from app.db.models import Search

logger = logging.getLogger(__name__)


def run_taxonomy_sync(
    db: Session, resolve_all: bool = False
) -> list[tuple[Search, ResolvedCategory | None]]:
    """Resolve fitment categories for active searches; persist what resolves.

    Returns (search, outcome) pairs for every search processed. Searches
    that don't resolve keep their existing category state (NULL stays NULL;
    with --all, a previously resolved category is only replaced by a new
    resolution, never erased by a failed one).
    """
    searches = queries.get_active_searches(db, cycle_type="nightly")
    if not resolve_all:
        searches = [s for s in searches if s.category_id is None]

    results: list[tuple[Search, ResolvedCategory | None]] = []
    for search in searches:
        resolved = resolve_fitment_category(db, search.query_text)
        if resolved is not None:
            search.category_id = resolved.category_id
            search.category_name = resolved.category_name
        results.append((search, resolved))
    db.commit()

    logger.info(
        "taxonomy-sync processed %d searches, %d resolved",
        len(results),
        sum(1 for _, r in results if r is not None),
    )
    return results
