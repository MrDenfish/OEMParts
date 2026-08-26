"""NHTSA vPIC client: VIN decoding and model lists, cached.

Free public API, no key. Fail-soft: any failure returns None and the
vehicle form falls back to free text. VINs are never logged in full —
use _redact() in every log line that mentions one.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import NhtsaModelCache, VinDecodeCache, utcnow

logger = logging.getLogger(__name__)

# httpx logs full request URLs at INFO; the decode URL contains the VIN.
# Force WARNING so the never-log-VINs house rule can't be broken by a
# future logging.basicConfig() in any entrypoint.
logging.getLogger("httpx").setLevel(logging.WARNING)

MODEL_CACHE_TTL_DAYS = 30
_TIMEOUT = 10
# 17 chars, alphanumeric, excluding I/O/Q per the VIN standard.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


@dataclass
class DecodedVin:
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    body_class: str | None


def is_valid_vin(vin: str) -> bool:
    return bool(_VIN_RE.match(vin.strip().upper()))


def _redact(vin: str) -> str:
    return f"{vin[:4]}…{vin[-4:]}" if len(vin) >= 8 else "…"


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def decode_vin(db: Session, vin: str) -> DecodedVin | None:
    """Decode a VIN via cache-then-NHTSA. None = invalid or transient failure."""
    vin = vin.strip().upper()
    if not _VIN_RE.match(vin):
        return None

    cached = db.get(VinDecodeCache, vin)
    if cached is not None:
        return DecodedVin(
            year=cached.year,
            make=cached.make,
            model=cached.model,
            trim=cached.trim,
            body_class=cached.body_class,
        )

    url = f"{settings.nhtsa_vpic_base_url}/vehicles/DecodeVinValues/{vin}?format=json"
    try:
        response = httpx.get(url, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("VIN decode failed for %s: %s", _redact(vin), exc)
        return None
    if response.status_code != 200:
        logger.warning("VIN decode HTTP %d for %s", response.status_code, _redact(vin))
        return None
    try:
        data = response.json()
    except ValueError:
        logger.warning("VIN decode non-JSON body for %s", _redact(vin))
        return None
    if not isinstance(data, dict):
        logger.warning("VIN decode unexpected body shape for %s", _redact(vin))
        return None
    results = data.get("Results") or []
    row: dict = results[0] if results and isinstance(results[0], dict) else {}

    year_str = _clean(row.get("ModelYear"))
    decoded = DecodedVin(
        year=int(year_str) if year_str and year_str.isdigit() else None,
        make=_clean(row.get("Make")),
        model=_clean(row.get("Model")),
        trim=_clean(row.get("Trim")),
        body_class=_clean(row.get("BodyClass")),
    )
    error_code = _clean(row.get("ErrorCode"))
    if error_code not in (None, "0"):
        logger.info("VIN decode ErrorCode %s for %s", error_code, _redact(vin))

    # Cache even an all-None decode: NHTSA doesn't know this VIN — final.
    try:
        db.add(
            VinDecodeCache(
                vin=vin,
                year=decoded.year,
                make=decoded.make,
                model=decoded.model,
                trim=decoded.trim,
                body_class=decoded.body_class,
                raw_json=json.dumps(row),
            )
        )
        db.commit()
    except Exception as exc:  # e.g. IntegrityError from a racing decode
        db.rollback()
        logger.warning("VIN decode cache write failed for %s: %s", _redact(vin), exc)
    return decoded


def get_models_for_make_year(db: Session, make: str, year: int) -> list[str] | None:
    """Model names for a make+year, cache-then-NHTSA. None = nothing available."""
    key_make = make.strip().lower()
    cached = db.get(NhtsaModelCache, (key_make, year))
    fresh_cutoff = utcnow() - timedelta(days=MODEL_CACHE_TTL_DAYS)
    if cached is not None and cached.cached_at >= fresh_cutoff:
        return json.loads(cached.models_json)

    url = (
        f"{settings.nhtsa_vpic_base_url}/vehicles/GetModelsForMakeYear"
        f"/make/{quote(make.strip(), safe='')}/modelyear/{year}?format=json"
    )
    try:
        response = httpx.get(url, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected NHTSA response shape")
        results = data.get("Results") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("NHTSA models fetch failed for %s %d: %s", make, year, exc)
        if cached is not None:  # serve stale rather than nothing
            return json.loads(cached.models_json)
        return None

    names = sorted(
        {
            name
            for entry in results
            if isinstance(entry, dict)
            and (name := _clean(entry.get("Model_Name"))) is not None
        }
    )
    if cached is None:
        cached = NhtsaModelCache(
            make=key_make, year=year, models_json=json.dumps(names)
        )
        db.add(cached)
    else:
        cached.models_json = json.dumps(names)
        cached.cached_at = utcnow()
    db.commit()
    return names
