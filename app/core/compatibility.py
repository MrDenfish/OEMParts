"""Fitment filtering: compatibility_filter builder + category resolution.

build_compatibility_filter produces the Browse API's semicolon-separated
filter string. resolve_fitment_category maps a search's query text to the
first eBay-suggested leaf category that lives under eBay Motors Parts &
Accessories (6028) and supports Year/Make/Model compatibility.
"""

import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.sources import ebay_taxonomy
from app.sources.ebay_taxonomy import (
    REFERENCE_FITMENT_CATEGORY_ID,
    get_compatibility_property_values,
)

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


def _fitment_key(value: str) -> str:
    """Lowercase alphanumerics only, so all spelling variants collide.

    'LAND ROVER', 'LandROVER', and 'Land rover' all normalize to
    'landrover' and therefore match the canonical 'Land Rover'.
    """
    return re.sub(r"[^a-z0-9]", "", value.lower())


def canonicalize_make(db: Session, make: str) -> str:
    """Map a make to eBay's canonical fitment-catalog spelling.

    eBay's compatibility_filter matches values case-sensitively — verified
    live 2026-09-14: Make "LAND ROVER" silently matched 48 listings where
    "Land Rover" matched 125. NHTSA VIN decodes return all-caps makes, so
    every make must pass through here before being stored on a vehicle.
    Returns the input unchanged when it is blank, unknown to the catalog,
    or the catalog lookup fails (fail-open — a fetch with the raw value
    still returns *some* results, and the value stays user-recognizable).
    """
    if not make.strip():
        return make
    values = get_compatibility_property_values(
        db, REFERENCE_FITMENT_CATEGORY_ID, "Make"
    )
    if not values:
        return make
    key = _fitment_key(make)
    for canonical in values:
        if _fitment_key(canonical) == key:
            return canonical
    return make


def canonicalize_model(db: Session, make: str, model: str) -> str:
    """Map a model to eBay's canonical spelling within one make.

    Pass the already-canonicalized make — the catalog scopes model lists
    by make ("lr4" → "LR4" only under "Land Rover"). Same fail-open
    semantics as canonicalize_make.
    """
    if not model.strip() or not make.strip():
        return model
    values = get_compatibility_property_values(
        db, REFERENCE_FITMENT_CATEGORY_ID, "Model", filter_make=make
    )
    if not values:
        return model
    key = _fitment_key(model)
    for canonical in values:
        if _fitment_key(canonical) == key:
            return canonical
    return model


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
