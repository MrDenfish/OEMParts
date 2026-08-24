"""eBay Taxonomy API client (Phase 2).

Resolves query text to leaf categories (get_category_suggestions) and
checks fitment support per category (get_compatibility_properties).
Compatibility-property answers are cached in the shared taxonomy_cache
table; the default category-tree id is cached in-process like the OAuth
token. Every API call is logged to api_quota_log as provider
"ebay_taxonomy".
"""

import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ApiQuotaLog, TaxonomyCache, utcnow
from app.sources.ebay_oauth import get_ebay_token

logger = logging.getLogger(__name__)

TAXONOMY_API_BASE = "https://api.ebay.com/commerce/taxonomy/v1"
TAXONOMY_SANDBOX_BASE = "https://api.sandbox.ebay.com/commerce/taxonomy/v1"

# Fitment support per category changes rarely; refresh monthly.
TAXONOMY_CACHE_TTL = timedelta(days=30)

# In-process cache for the marketplace's category tree id (a small,
# effectively static value — one API call per process lifetime).
_tree_id_cache: str | None = None

# Fitment (Motors parts) categories live in the eBay Motors category tree,
# not the general marketplace tree. Verified live 2026-08-24: the EBAY_US
# tree ("0") rejects Motors category ids (error 62005) and suggests
# toy/collectible categories for part queries; EBAY_MOTORS_US resolves to
# tree "100", whose suggestions match real Browse listing categories.
FITMENT_TREE_MARKETPLACE_ID = "EBAY_MOTORS_US"


@dataclass
class CategorySuggestion:
    """One suggestion from get_category_suggestions, root-to-leaf ancestors."""

    category_id: str
    category_name: str
    ancestor_ids: list[str]


def _get_taxonomy_base() -> str:
    """Return the correct Taxonomy API base URL for the configured env."""
    if settings.ebay_env == "sandbox":
        return TAXONOMY_SANDBOX_BASE
    return TAXONOMY_API_BASE


def _log_api_call(db: Session, status_code: int | None) -> None:
    """Record a Taxonomy API call in the api_quota_log table."""
    db.add(ApiQuotaLog(provider="ebay_taxonomy", status_code=status_code))
    db.flush()


def _request_json(db: Session, path: str, params: dict[str, str]) -> dict | None:
    """GET a Taxonomy API endpoint; return parsed JSON or None on any error.

    Non-200 responses are logged and treated as None — callers decide what
    a missing answer means (e.g. "category does not support fitment").
    """
    token = get_ebay_token(db)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": settings.ebay_marketplace_id,
    }
    status_code: int | None = None
    try:
        with httpx.Client() as client:
            response = client.get(
                f"{_get_taxonomy_base()}{path}",
                params=params,
                headers=headers,
                timeout=30.0,
            )
            status_code = response.status_code
        _log_api_call(db, status_code)
        if status_code != 200:
            logger.warning(
                "eBay Taxonomy API returned %d for %s: %s",
                status_code,
                path,
                response.text[:200],
            )
            return None
        try:
            return response.json()
        except ValueError as exc:
            logger.warning(
                "eBay Taxonomy API returned invalid JSON for %s: %s",
                path,
                exc,
            )
            return None
    except httpx.HTTPError as exc:
        logger.warning("eBay Taxonomy API error for %s: %s", path, exc)
        _log_api_call(db, status_code)
        return None


def get_default_tree_id(db: Session) -> str | None:
    """Return the eBay Motors category tree id used for fitment lookups.

    Fitment (Motors parts) categories live in the separate eBay Motors tree,
    not the general marketplace tree — see FITMENT_TREE_MARKETPLACE_ID.
    Cached in-process (a small, effectively static value).
    """
    global _tree_id_cache
    if _tree_id_cache is not None:
        return _tree_id_cache
    data = _request_json(
        db,
        "/get_default_category_tree_id",
        {"marketplace_id": FITMENT_TREE_MARKETPLACE_ID},
    )
    if data is None or "categoryTreeId" not in data:
        return None
    _tree_id_cache = str(data["categoryTreeId"])
    return _tree_id_cache


def get_category_suggestions(db: Session, query: str) -> list[CategorySuggestion]:
    """Return eBay's ranked category suggestions for a query string."""
    tree_id = get_default_tree_id(db)
    if tree_id is None:
        return []
    data = _request_json(
        db, f"/category_tree/{tree_id}/get_category_suggestions", {"q": query}
    )
    if data is None:
        return []
    suggestions: list[CategorySuggestion] = []
    for entry in data.get("categorySuggestions", []):
        category = entry.get("category", {})
        category_id = category.get("categoryId")
        category_name = category.get("categoryName")
        if not category_id or not category_name:
            continue
        ancestors = [
            str(a["categoryId"])
            for a in entry.get("categoryTreeNodeAncestors", [])
            if a.get("categoryId")
        ]
        suggestions.append(
            CategorySuggestion(
                category_id=str(category_id),
                category_name=str(category_name),
                ancestor_ids=ancestors,
            )
        )
    return suggestions


def _get_cached_properties(db: Session, category_id: str) -> list[str] | None:
    """Return cached property names for a category, or None on miss/expired."""
    row = (
        db.query(TaxonomyCache)
        .filter(
            TaxonomyCache.category_id == category_id,
            TaxonomyCache.marketplace == settings.ebay_marketplace_id,
            TaxonomyCache.year.is_(None),
            TaxonomyCache.make.is_(None),
            TaxonomyCache.model.is_(None),
        )
        .first()
    )
    if row is None or row.raw_json is None:
        return None
    if row.refreshed_at < utcnow() - TAXONOMY_CACHE_TTL:
        return None
    try:
        properties = json.loads(row.raw_json).get("properties")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(properties, list):
        return None
    return [str(p) for p in properties]


def _store_cached_properties(
    db: Session, category_id: str, properties: list[str]
) -> None:
    """Insert or refresh the taxonomy_cache row for a category."""
    row = (
        db.query(TaxonomyCache)
        .filter(
            TaxonomyCache.category_id == category_id,
            TaxonomyCache.marketplace == settings.ebay_marketplace_id,
            TaxonomyCache.year.is_(None),
            TaxonomyCache.make.is_(None),
            TaxonomyCache.model.is_(None),
        )
        .first()
    )
    payload = json.dumps({"properties": properties})
    if row is None:
        row = TaxonomyCache(
            category_id=category_id,
            marketplace=settings.ebay_marketplace_id,
            raw_json=payload,
        )
        db.add(row)
    else:
        row.raw_json = payload
        row.refreshed_at = utcnow()
    db.flush()


def get_compatibility_properties(db: Session, category_id: str) -> list[str]:
    """Return the compatibility property names a category supports.

    Empty list means "no fitment support" (including eBay error responses,
    which are cached too so unsupported categories are asked about once).
    Reads through taxonomy_cache with a 30-day TTL.
    """
    cached = _get_cached_properties(db, category_id)
    if cached is not None:
        return cached
    tree_id = get_default_tree_id(db)
    if tree_id is None:
        return []
    data = _request_json(
        db,
        f"/category_tree/{tree_id}/get_compatibility_properties",
        {"category_id": category_id},
    )
    if data is None:
        properties: list[str] = []
    else:
        properties = [
            str(p["name"])
            for p in data.get("compatibilityProperties", [])
            if p.get("name")
        ]
    _store_cached_properties(db, category_id, properties)
    return properties
