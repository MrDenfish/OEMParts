"""eBay Browse getItem client: structured Item Specifics per listing.

The search endpoint's summaries carry no brand data; sellers game titles
(live example: "Amk Air Ride Compressor" with Brand: Dorman). One getItem
call per listing captures Brand / MPN / OE-OEM Part Number as ground truth.
Fail-soft: transient errors return None so the caller retries next cycle.
"""

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ApiQuotaLog
from app.sources.ebay_oauth import get_ebay_token

logger = logging.getLogger(__name__)

ITEM_API_URL = "https://api.ebay.com/buy/browse/v1/item/"
ITEM_SANDBOX_URL = "https://api.sandbox.ebay.com/buy/browse/v1/item/"
_MAX_LEN = 100
# 404 = listing gone, 410 = permanently removed: both are final answers,
# not transient failures — treat as success-with-nothing so we never retry.
_GONE_STATUSES = (404, 410)


@dataclass
class ItemAspects:
    brand: str | None
    mpn: str | None
    oe_part_number: str | None


def _get_item_url() -> str:
    if settings.ebay_env == "sandbox":
        return ITEM_SANDBOX_URL
    return ITEM_API_URL


def _log_api_call(db: Session, status_code: int | None) -> None:
    # Commit (not flush): quota accounting must survive a caller's later
    # rollback — the enrichment loop rolls back on per-listing failures,
    # and a flushed-but-uncommitted row would be silently discarded,
    # undercounting usage against the 5,000/day budget.
    db.add(ApiQuotaLog(provider="ebay_item", status_code=status_code))
    db.commit()


def _clip(value: object) -> str | None:
    """Truncate a trusted-shape string to _MAX_LEN; anything else is None.

    eBay aspect values are supposed to be strings, but a poison listing
    (unexpected API response shape) could hand us a list/dict/number here.
    Fail soft rather than raising AttributeError on .strip().
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:_MAX_LEN] if value else None


def fetch_item_aspects(db: Session, ebay_item_id: str) -> ItemAspects | None:
    """Fetch Brand/MPN/OE# for one listing. None = transient failure (retry).

    An ItemAspects result (even all-None) means the lookup is final — the
    caller stamps aspects_fetched_at and never asks again.
    """
    token = get_ebay_token(db)
    # ebay_item_id is already eBay's full RESTful ID (e.g. "v1|267701844481|0")
    # verbatim from the Browse API — percent-encode it as one opaque path
    # segment rather than re-wrapping it in another "v1|...|0" shell.
    url = f"{_get_item_url()}{quote(ebay_item_id, safe='')}"
    try:
        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        _log_api_call(db, None)
        logger.warning("getItem failed for %s: %s", ebay_item_id, exc)
        return None

    _log_api_call(db, response.status_code)
    if response.status_code in _GONE_STATUSES:
        return ItemAspects(brand=None, mpn=None, oe_part_number=None)
    if response.status_code != 200:
        logger.warning("getItem HTTP %d for %s", response.status_code, ebay_item_id)
        return None

    try:
        data = response.json()
    except ValueError:
        logger.warning("getItem non-JSON body for %s", ebay_item_id)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "getItem unexpected JSON shape for %s: %r", ebay_item_id, type(data)
        )
        return None

    aspects = {
        a.get("name"): a.get("value")
        for a in data.get("localizedAspects", [])
        if isinstance(a, dict)
    }
    brand = data.get("brand") or aspects.get("Brand")
    return ItemAspects(
        brand=_clip(brand),
        mpn=_clip(aspects.get("Manufacturer Part Number")),
        oe_part_number=_clip(aspects.get("OE/OEM Part Number")),
    )
