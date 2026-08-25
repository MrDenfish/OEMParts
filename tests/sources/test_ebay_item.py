"""Tests for the eBay getItem aspects client."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ApiQuotaLog
from app.sources import ebay_item
from app.sources.ebay_item import ItemAspects, fetch_item_aspects


def _response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload or {},
        request=httpx.Request("GET", "https://api.ebay.com/x"),
    )


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ebay_item, "get_ebay_token", lambda db: "test-token")


def _quota_rows(db_session: Session) -> list[ApiQuotaLog]:
    return list(db_session.execute(select(ApiQuotaLog)).scalars())


def test_top_level_brand_and_aspects_all_extracted(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    payload = {
        "brand": "Dorman",
        "localizedAspects": [
            {"name": "Brand", "value": "Dorman"},
            {"name": "Manufacturer Part Number", "value": "949-919"},
            {"name": "OE/OEM Part Number", "value": "LR124471"},
        ],
    }
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(200, payload))

    result = fetch_item_aspects(db_session, "123456")

    assert result == ItemAspects(
        brand="Dorman", mpn="949-919", oe_part_number="LR124471"
    )


def test_no_top_level_brand_uses_brand_aspect(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    payload = {
        "localizedAspects": [
            {"name": "Brand", "value": "Dorman"},
        ],
    }
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(200, payload))

    result = fetch_item_aspects(db_session, "123456")

    assert result == ItemAspects(brand="Dorman", mpn=None, oe_part_number=None)


def test_no_aspects_at_all_is_success_with_all_none(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(200, {}))

    result = fetch_item_aspects(db_session, "123456")

    assert result == ItemAspects(brand=None, mpn=None, oe_part_number=None)


def test_404_is_success_with_nothing(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(404))

    result = fetch_item_aspects(db_session, "123456")

    assert result == ItemAspects(brand=None, mpn=None, oe_part_number=None)


def test_410_is_success_with_nothing(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(410))

    result = fetch_item_aspects(db_session, "123456")

    assert result == ItemAspects(brand=None, mpn=None, oe_part_number=None)


def test_500_returns_none_for_retry(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(500))

    result = fetch_item_aspects(db_session, "123456")

    assert result is None


def test_connect_error_returns_none_for_retry(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(ebay_item.httpx, "get", _raise)

    result = fetch_item_aspects(db_session, "123456")

    assert result is None


def test_long_values_truncated_to_100_chars(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    long_value = "X" * 150
    payload = {
        "brand": long_value,
        "localizedAspects": [
            {"name": "Manufacturer Part Number", "value": long_value},
            {"name": "OE/OEM Part Number", "value": long_value},
        ],
    }
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(200, payload))

    result = fetch_item_aspects(db_session, "123456")

    assert result is not None
    assert result.brand is not None and len(result.brand) == 100
    assert result.mpn is not None and len(result.mpn) == 100
    assert result.oe_part_number is not None and len(result.oe_part_number) == 100


def test_every_call_inserts_one_quota_log_row_200(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(200, {}))

    fetch_item_aspects(db_session, "123456")

    rows = _quota_rows(db_session)
    assert len(rows) == 1
    assert rows[0].provider == "ebay_item"
    assert rows[0].status_code == 200


def test_every_call_inserts_one_quota_log_row_404(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(404))

    fetch_item_aspects(db_session, "123456")

    rows = _quota_rows(db_session)
    assert len(rows) == 1
    assert rows[0].provider == "ebay_item"
    assert rows[0].status_code == 404


def test_every_call_inserts_one_quota_log_row_500(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    monkeypatch.setattr(ebay_item.httpx, "get", lambda *a, **k: _response(500))

    fetch_item_aspects(db_session, "123456")

    rows = _quota_rows(db_session)
    assert len(rows) == 1
    assert rows[0].provider == "ebay_item"
    assert rows[0].status_code == 500


def test_every_call_inserts_one_quota_log_row_connect_error(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(ebay_item.httpx, "get", _raise)

    fetch_item_aspects(db_session, "123456")

    rows = _quota_rows(db_session)
    assert len(rows) == 1
    assert rows[0].provider == "ebay_item"
    assert rows[0].status_code is None
