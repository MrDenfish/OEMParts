"""Tests for get_compatibility_property_values (canonical fitment values).

The HTTP layer (_request_json) is monkeypatched. These tests cover parsing,
the compat_value_cache read-through (30-day TTL), and serve-stale-on-failure
semantics (mirroring the NHTSA model cache).
"""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.models import CompatValueCache, utcnow
from app.sources import ebay_taxonomy


@pytest.fixture(autouse=True)
def _pin_tree_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the tree-id lookup — these tests exercise the values endpoint."""
    monkeypatch.setattr(ebay_taxonomy, "get_default_tree_id", lambda db: "100")


def _fake_response(values: list[str]) -> dict:
    return {"compatibilityPropertyValues": [{"value": v} for v in values]}


def test_fetches_parses_and_caches_make_values(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_request(db, path, params):
        calls.append(params)
        return _fake_response(["Land Rover", "BMW", "Toyota"])

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)

    values = ebay_taxonomy.get_compatibility_property_values(
        db_session, "177697", "Make"
    )
    assert values == ["Land Rover", "BMW", "Toyota"]
    assert len(calls) == 1
    assert calls[0]["compatibility_property"] == "Make"

    # Second call is served from compat_value_cache — no new API call.
    again = ebay_taxonomy.get_compatibility_property_values(
        db_session, "177697", "Make"
    )
    assert again == ["Land Rover", "BMW", "Toyota"]
    assert len(calls) == 1


def test_model_values_are_filtered_by_make_and_cached_separately(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_request(db, path, params):
        calls.append(params)
        return _fake_response(["LR4", "Range Rover"])

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)

    values = ebay_taxonomy.get_compatibility_property_values(
        db_session, "177697", "Model", filter_make="Land Rover"
    )
    assert values == ["LR4", "Range Rover"]
    assert calls[0]["filter"] == "Make:Land Rover"

    # A different make must NOT hit the same cache row.
    monkeypatch.setattr(
        ebay_taxonomy,
        "_request_json",
        lambda db, path, params: _fake_response(["3 Series"]),
    )
    bmw = ebay_taxonomy.get_compatibility_property_values(
        db_session, "177697", "Model", filter_make="BMW"
    )
    assert bmw == ["3 Series"]


def test_serves_stale_cache_when_api_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = CompatValueCache(
        property="Make",
        category_id="177697",
        filter_make="",
        marketplace="EBAY_US",
        values_json='["Land Rover"]',
        refreshed_at=utcnow() - timedelta(days=90),
    )
    db_session.add(row)
    db_session.commit()

    monkeypatch.setattr(ebay_taxonomy, "_request_json", lambda db, path, params: None)

    values = ebay_taxonomy.get_compatibility_property_values(
        db_session, "177697", "Make"
    )
    assert values == ["Land Rover"]


def test_returns_none_on_miss_plus_api_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ebay_taxonomy, "_request_json", lambda db, path, params: None)
    values = ebay_taxonomy.get_compatibility_property_values(
        db_session, "177697", "Make"
    )
    assert values is None
