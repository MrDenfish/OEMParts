"""Fitment filtering: compatibility_filter builder + category resolution.

build_compatibility_filter produces the Browse API's semicolon-separated
filter string. resolve_fitment_category maps a search's query text to the
first eBay-suggested leaf category that lives under eBay Motors Parts &
Accessories (6028) and supports Year/Make/Model compatibility.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.sources import ebay_taxonomy

logger = logging.getLogger(__name__)

# eBay Motors Parts & Accessories — the root under which fitment applies.
# A marketplace fact, not configuration, so it lives in code (see spec §5.3).
EBAY_MOTORS_PARTS_CATEGORY_ID = "6028"

REQUIRED_FITMENT_PROPERTIES = {"Year", "Make", "Model"}


@dataclass
class ResolvedCategory:
    """A leaf category confirmed to support Year/Make/Model fitment."""

    category_id: str
    category_name: str


def build_compatibility_filter(year: int, make: str, model: str) -> str:
    """Build the Browse API compatibility_filter string (semicolon-separated).

    Example: 'Year:2012;Make:Land Rover;Model:LR4'
    """
    return f"Year:{year};Make:{make};Model:{model}"


def resolve_fitment_category(db: Session, query_text: str) -> ResolvedCategory | None:
    """Resolve query text to a fitment-supporting eBay Motors leaf category.

    Walks eBay's ranked suggestions and returns the first that (a) has 6028
    in its ancestor path and (b) supports Year/Make/Model compatibility
    properties. Returns None when nothing qualifies or on any API failure —
    resolution must never block search creation.
    """
    try:
        suggestions = ebay_taxonomy.get_category_suggestions(db, query_text)
        for suggestion in suggestions:
            if EBAY_MOTORS_PARTS_CATEGORY_ID not in suggestion.ancestor_ids:
                continue
            properties = ebay_taxonomy.get_compatibility_properties(
                db, suggestion.category_id
            )
            if REQUIRED_FITMENT_PROPERTIES <= set(properties):
                logger.info(
                    "Resolved '%s' to category %s (%s)",
                    query_text,
                    suggestion.category_id,
                    suggestion.category_name,
                )
                return ResolvedCategory(
                    category_id=suggestion.category_id,
                    category_name=suggestion.category_name,
                )
        logger.info("No fitment category found for query '%s'", query_text)
        return None
    except Exception:
        logger.warning(
            "Category resolution failed for query '%s'", query_text, exc_info=True
        )
        return None
