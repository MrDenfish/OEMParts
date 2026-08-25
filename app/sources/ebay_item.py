"""eBay Browse getItem client: structured Item Specifics per listing.

The search endpoint's summaries carry no brand data; sellers game titles
(live example: "Amk Air Ride Compressor" with Brand: Dorman). One getItem
call per listing captures Brand / MPN / OE-OEM Part Number as ground truth.
Fail-soft: transient errors return None so the caller retries next cycle.
"""

import logging
from dataclasses import dataclass

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
    db.add(ApiQuotaLog(provider="ebay_item", status_code=status_code))
    db.flush()


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:_MAX_LEN] if value else None


def fetch_item_aspects(db: Session, ebay_item_id: str) -> ItemAspects | None:
    """Fetch Brand/MPN/OE# for one listing. None = transient failure (retry).

    An ItemAspects result (even all-None) means the lookup is final — the
    caller stamps aspects_fetched_at and never asks again.
    """
    token = get_ebay_token(db)
    url = f"{_get_item_url()}v1%7C{ebay_item_id}%7C0"
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
        return ItemAspects(brand=None, mpn=None, oe_part_number=None)

    aspects = {
        a.get("name"): a.get("value")
        for a in data.get("localizedAspects", [])
        if isinstance(a, dict)
    }
    brand = data.get("brand") or aspects.get("Brand")
    return ItemAspects(
        brand=_clip(brand if isinstance(brand, str) else None),
        mpn=_clip(aspects.get("Manufacturer Part Number")),
        oe_part_number=_clip(aspects.get("OE/OEM Part Number")),
    )
