"""Tests for the NHTSA vPIC client (VIN decode + model lists)."""

import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import NhtsaModelCache, VinDecodeCache, utcnow
from app.sources import nhtsa_vpic
from app.sources.nhtsa_vpic import (
    DecodedVin,
    decode_vin,
    get_models_for_make_year,
    is_valid_vin,
)

# A syntactically valid 17-char VIN (no I/O/Q).
VALID_VIN = "5LMJJ2H57CEJ12345"


def _response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload if payload is not None else {},
        request=httpx.Request("GET", "https://vpic.nhtsa.dot.gov/api/x"),
    )


def _fail_if_called(*args, **kwargs):
    raise AssertionError("httpx.get should not have been called")


# ---------------------------------------------------------------------------
# is_valid_vin
# ---------------------------------------------------------------------------


def test_is_valid_vin_accepts_clean_17_char_vin() -> None:
    assert is_valid_vin(VALID_VIN) is True


def test_is_valid_vin_rejects_wrong_length() -> None:
    assert is_valid_vin("1234567890ABCDEF") is False  # 16 chars
    assert is_valid_vin("1234567890ABCDEFGH") is False  # 18 chars


# ---------------------------------------------------------------------------
# decode_vin — case 1: invalid VINs never reach the network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_vin",
    [
        "1234567890ABCDEF",  # 16 chars
        "1234567890ABCDEFGH",  # 18 chars
        "1234567890123456I",  # 17 chars but contains I
        "",  # empty
    ],
)
def test_decode_vin_invalid_format_returns_none_without_http_call(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, bad_vin: str
) -> None:
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _fail_if_called)

    result = decode_vin(db_session, bad_vin)

    assert result is None


# ---------------------------------------------------------------------------
# decode_vin — case 2: clean decode
# ---------------------------------------------------------------------------


def test_decode_vin_clean_decode_returns_values_and_writes_cache(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    payload = {
        "Results": [
            {
                "ModelYear": "2012",
                "Make": "LAND ROVER",
                "Model": "LR4",
                "Trim": "HSE",
                "BodyClass": "Sport Utility Vehicle (SUV)/Multi-Purpose Vehicle (MPV)",
                "ErrorCode": "0",
            }
        ]
    }
    monkeypatch.setattr(
        nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, payload)
    )

    result = decode_vin(db_session, VALID_VIN)

    assert result == DecodedVin(
        year=2012,
        make="LAND ROVER",
        model="LR4",
        trim="HSE",
        body_class="Sport Utility Vehicle (SUV)/Multi-Purpose Vehicle (MPV)",
    )

    cached = db_session.get(VinDecodeCache, VALID_VIN)
    assert cached is not None
    assert cached.raw_json is not None
    assert cached.decoded_at is not None


def test_decode_vin_forces_httpx_logger_to_warning(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """Module import must clamp the httpx logger so INFO-level full-URL

    (VIN-containing) request logs can never leak, even if some future
    entrypoint calls logging.basicConfig().
    """
    payload = {
        "Results": [
            {
                "ModelYear": "2012",
                "Make": "LAND ROVER",
                "Model": "LR4",
                "Trim": "HSE",
                "BodyClass": "SUV",
                "ErrorCode": "0",
            }
        ]
    }
    monkeypatch.setattr(
        nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, payload)
    )

    decode_vin(db_session, VALID_VIN)

    assert logging.getLogger("httpx").level == logging.WARNING


# ---------------------------------------------------------------------------
# decode_vin — case 3: ErrorCode present but Y/M/M usable -> partial decode
# ---------------------------------------------------------------------------


def test_decode_vin_error_code_but_usable_fields_returned_anyway(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    payload = {
        "Results": [
            {
                "ModelYear": "2012",
                "Make": "LAND ROVER",
                "Model": "LR4",
                "Trim": "",
                "BodyClass": "",
                "ErrorCode": "6",
            }
        ]
    }
    monkeypatch.setattr(
        nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, payload)
    )

    result = decode_vin(db_session, VALID_VIN)

    assert result == DecodedVin(
        year=2012, make="LAND ROVER", model="LR4", trim=None, body_class=None
    )


# ---------------------------------------------------------------------------
# decode_vin — case 4: cache hit skips the network entirely
# ---------------------------------------------------------------------------


def test_decode_vin_cache_hit_skips_http_call(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    db_session.add(
        VinDecodeCache(
            vin=VALID_VIN,
            year=2012,
            make="LAND ROVER",
            model="LR4",
            trim="HSE",
            body_class="SUV",
            raw_json="{}",
        )
    )
    db_session.commit()
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _fail_if_called)

    result = decode_vin(db_session, VALID_VIN)

    assert result == DecodedVin(
        year=2012, make="LAND ROVER", model="LR4", trim="HSE", body_class="SUV"
    )


# ---------------------------------------------------------------------------
# decode_vin — case 5: all-empty decode is cached as final (no repeat calls)
# ---------------------------------------------------------------------------


def test_decode_vin_all_empty_decode_is_cached_and_final(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    calls: list[str] = []

    def _fake_get(url, *a, **k):
        calls.append(url)
        return _response(
            200,
            {
                "Results": [
                    {
                        "ModelYear": "",
                        "Make": "",
                        "Model": "",
                        "Trim": "",
                        "BodyClass": "",
                        "ErrorCode": "0",
                    }
                ]
            },
        )

    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _fake_get)

    first = decode_vin(db_session, VALID_VIN)
    second = decode_vin(db_session, VALID_VIN)

    assert first == DecodedVin(
        year=None, make=None, model=None, trim=None, body_class=None
    )
    assert second == first
    assert len(calls) == 1  # second call served from cache


# ---------------------------------------------------------------------------
# decode_vin — case 6: transient failures return None, no cache row, and
# the full VIN never appears in logs.
# ---------------------------------------------------------------------------


def test_decode_vin_http_500_returns_none_no_cache_row_redacted_log(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", lambda *a, **k: _response(500))

    with caplog.at_level(logging.WARNING, logger="app.sources.nhtsa_vpic"):
        result = decode_vin(db_session, VALID_VIN)

    assert result is None
    assert db_session.get(VinDecodeCache, VALID_VIN) is None
    assert len(caplog.records) >= 1
    assert VALID_VIN not in caplog.text
    assert "5LMJ" in caplog.text  # redacted prefix is fine to log


def test_decode_vin_connect_error_returns_none_no_cache_row_redacted_log(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _raise)

    with caplog.at_level(logging.WARNING, logger="app.sources.nhtsa_vpic"):
        result = decode_vin(db_session, VALID_VIN)

    assert result is None
    assert db_session.get(VinDecodeCache, VALID_VIN) is None
    assert len(caplog.records) >= 1
    assert VALID_VIN not in caplog.text
    assert "5LMJ" in caplog.text


def test_decode_vin_non_dict_body_returns_none_no_cache_row(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200 whose JSON body is a list/scalar (not the expected dict shape)

    must be treated as a failure rather than raising AttributeError past
    the existing ValueError catch.
    """
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, [1, 2]))

    with caplog.at_level(logging.WARNING, logger="app.sources.nhtsa_vpic"):
        result = decode_vin(db_session, VALID_VIN)

    assert result is None
    assert db_session.get(VinDecodeCache, VALID_VIN) is None
    assert VALID_VIN not in caplog.text


def test_decode_vin_cache_write_failure_still_returns_decoded_result(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A commit() failure (e.g. a racing decode hitting a unique-key

    IntegrityError) must not turn into a 500 — the caller still gets the
    decoded value, and the write failure is logged without leaking the
    full VIN.
    """
    payload = {
        "Results": [
            {
                "ModelYear": "2012",
                "Make": "LAND ROVER",
                "Model": "LR4",
                "Trim": "HSE",
                "BodyClass": "SUV",
                "ErrorCode": "0",
            }
        ]
    }
    monkeypatch.setattr(
        nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, payload)
    )

    real_commit = db_session.commit
    calls = {"n": 0}

    def _commit_once_then_raise():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated IntegrityError")
        return real_commit()

    monkeypatch.setattr(db_session, "commit", _commit_once_then_raise)

    with caplog.at_level(logging.WARNING, logger="app.sources.nhtsa_vpic"):
        result = decode_vin(db_session, VALID_VIN)

    assert result == DecodedVin(
        year=2012, make="LAND ROVER", model="LR4", trim="HSE", body_class="SUV"
    )
    assert any("cache write failed" in record.message for record in caplog.records)
    assert VALID_VIN not in caplog.text


# ---------------------------------------------------------------------------
# decode_vin — case 7: lowercase input with surrounding whitespace normalized
# ---------------------------------------------------------------------------


def test_decode_vin_normalizes_lowercase_and_whitespace(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    payload = {
        "Results": [
            {
                "ModelYear": "2012",
                "Make": "LAND ROVER",
                "Model": "LR4",
                "Trim": "HSE",
                "BodyClass": "SUV",
                "ErrorCode": "0",
            }
        ]
    }
    captured_urls: list[str] = []

    def _fake_get(url, *a, **k):
        captured_urls.append(url)
        return _response(200, payload)

    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _fake_get)

    result = decode_vin(db_session, f"  {VALID_VIN.lower()}  ")

    assert result == DecodedVin(
        year=2012, make="LAND ROVER", model="LR4", trim="HSE", body_class="SUV"
    )
    assert captured_urls == [
        f"{settings.nhtsa_vpic_base_url}/vehicles/DecodeVinValues/{VALID_VIN}?format=json"
    ]
    cached = db_session.get(VinDecodeCache, VALID_VIN)
    assert cached is not None


# ---------------------------------------------------------------------------
# get_models_for_make_year — case 8: API success, sorted + deduped, cached
# ---------------------------------------------------------------------------


def test_get_models_for_make_year_success_sorted_deduped_and_cached(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    payload = {
        "Results": [
            {"Model_Name": "LR4"},
            {"Model_Name": "Range Rover"},
            {"Model_Name": "LR4"},
        ]
    }
    monkeypatch.setattr(
        nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, payload)
    )

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result == ["LR4", "Range Rover"]
    cached = db_session.get(NhtsaModelCache, ("land rover", 2012))
    assert cached is not None
    assert cached.models_json == '["LR4", "Range Rover"]'


# ---------------------------------------------------------------------------
# get_models_for_make_year — case 9: fresh cache hit skips the network
# ---------------------------------------------------------------------------


def test_get_models_for_make_year_fresh_cache_hit_skips_http_call(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    db_session.add(
        NhtsaModelCache(
            make="land rover",
            year=2012,
            models_json='["LR4", "Range Rover"]',
            cached_at=utcnow(),
        )
    )
    db_session.commit()
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _fail_if_called)

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result == ["LR4", "Range Rover"]


# ---------------------------------------------------------------------------
# get_models_for_make_year — case 10: stale cache re-fetches and updates
# ---------------------------------------------------------------------------


def test_get_models_for_make_year_stale_cache_refetches_and_updates(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    stale_at = utcnow() - timedelta(days=31)
    db_session.add(
        NhtsaModelCache(
            make="land rover",
            year=2012,
            models_json='["OLD MODEL"]',
            cached_at=stale_at,
        )
    )
    db_session.commit()

    payload = {"Results": [{"Model_Name": "LR4"}, {"Model_Name": "Discovery"}]}
    monkeypatch.setattr(
        nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, payload)
    )

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result == ["Discovery", "LR4"]
    cached = db_session.get(NhtsaModelCache, ("land rover", 2012))
    assert cached is not None
    assert cached.models_json == '["Discovery", "LR4"]'
    assert cached.cached_at > stale_at


# ---------------------------------------------------------------------------
# get_models_for_make_year — case 11: stale cache + API error -> serve-stale
# ---------------------------------------------------------------------------


def test_get_models_for_make_year_stale_cache_plus_api_error_serves_stale(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    stale_at = utcnow() - timedelta(days=31)
    db_session.add(
        NhtsaModelCache(
            make="land rover",
            year=2012,
            models_json='["OLD MODEL"]',
            cached_at=stale_at,
        )
    )
    db_session.commit()

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(nhtsa_vpic.httpx, "get", _raise)

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result == ["OLD MODEL"]


# ---------------------------------------------------------------------------
# get_models_for_make_year — case 12: no cache + API error -> None
# ---------------------------------------------------------------------------


def test_get_models_for_make_year_no_cache_plus_api_error_returns_none(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", lambda *a, **k: _response(500))

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result is None


# ---------------------------------------------------------------------------
# get_models_for_make_year — case 13: non-dict body treated as failure
# ---------------------------------------------------------------------------


def test_get_models_for_make_year_non_dict_body_serves_stale_when_cached(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    stale_at = utcnow() - timedelta(days=31)
    db_session.add(
        NhtsaModelCache(
            make="land rover",
            year=2012,
            models_json='["OLD MODEL"]',
            cached_at=stale_at,
        )
    )
    db_session.commit()
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, [1, 2]))

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result == ["OLD MODEL"]


def test_get_models_for_make_year_non_dict_body_no_cache_returns_none(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(nhtsa_vpic.httpx, "get", lambda *a, **k: _response(200, [1, 2]))

    result = get_models_for_make_year(db_session, "Land Rover", 2012)

    assert result is None
