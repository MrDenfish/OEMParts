"""Tests for the eBay Taxonomy API client.

The HTTP layer (_request_json) is monkeypatched; these tests cover parsing,
in-process tree-id caching, and taxonomy_cache read-through behavior.
"""

import json
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import TaxonomyCache, utcnow
from app.sources import ebay_taxonomy


@pytest.fixture(autouse=True)
def _reset_tree_id_cache():
    ebay_taxonomy._tree_id_cache = None
    yield
    ebay_taxonomy._tree_id_cache = None


def test_get_default_tree_id_parses_and_caches(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_request(db, path, params):
        calls.append(path)
        return {"categoryTreeId": "0", "categoryTreeVersion": "129"}

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)
    assert ebay_taxonomy.get_default_tree_id(db_session) == "0"
    # Second call served from the in-process cache — no new request
    assert ebay_taxonomy.get_default_tree_id(db_session) == "0"
    assert len(calls) == 1


def test_get_default_tree_id_returns_none_on_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ebay_taxonomy, "_request_json", lambda db, path, params: None)
    assert ebay_taxonomy.get_default_tree_id(db_session) is None


def test_get_category_suggestions_parses_ancestors(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "categorySuggestions": [
            {
                "category": {"categoryId": "184656", "categoryName": "Water Pumps"},
                "categoryTreeNodeAncestors": [
                    {"categoryId": "6000", "categoryName": "eBay Motors"},
                    {"categoryId": "6028", "categoryName": "Parts & Accessories"},
                ],
            },
            {
                "category": {"categoryId": "11700", "categoryName": "Home & Garden"},
                "categoryTreeNodeAncestors": [],
            },
        ]
    }

    def fake_request(db, path, params):
        if path == "/get_default_category_tree_id":
            return {"categoryTreeId": "0"}
        assert path == "/category_tree/0/get_category_suggestions"
        assert params == {"q": "water pump"}
        return payload

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)
    suggestions = ebay_taxonomy.get_category_suggestions(db_session, "water pump")
    assert [s.category_id for s in suggestions] == ["184656", "11700"]
    assert suggestions[0].category_name == "Water Pumps"
    assert suggestions[0].ancestor_ids == ["6000", "6028"]
    assert suggestions[1].ancestor_ids == []


def test_get_category_suggestions_empty_on_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(db, path, params):
        if path == "/get_default_category_tree_id":
            return {"categoryTreeId": "0"}
        return None

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)
    assert ebay_taxonomy.get_category_suggestions(db_session, "anything") == []


def test_compatibility_properties_cache_miss_calls_api_and_writes_cache(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_request(db, path, params):
        if path == "/get_default_category_tree_id":
            return {"categoryTreeId": "0"}
        calls.append(params["category_id"])
        return {
            "compatibilityProperties": [
                {"name": "Year"},
                {"name": "Make"},
                {"name": "Model"},
                {"name": "Trim"},
            ]
        }

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)
    props = ebay_taxonomy.get_compatibility_properties(db_session, "184656")
    assert props == ["Year", "Make", "Model", "Trim"]
    assert calls == ["184656"]

    row = (
        db_session.query(TaxonomyCache)
        .filter(
            TaxonomyCache.category_id == "184656",
            TaxonomyCache.marketplace == settings.ebay_marketplace_id,
        )
        .one()
    )
    assert json.loads(row.raw_json)["properties"] == ["Year", "Make", "Model", "Trim"]


def test_compatibility_properties_cache_hit_skips_api(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        TaxonomyCache(
            category_id="184656",
            marketplace=settings.ebay_marketplace_id,
            raw_json=json.dumps({"properties": ["Year", "Make", "Model"]}),
            refreshed_at=utcnow(),
        )
    )
    db_session.commit()

    def fail_request(db, path, params):  # pragma: no cover - must not be called
        raise AssertionError("API should not be called on cache hit")

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fail_request)
    assert ebay_taxonomy.get_compatibility_properties(db_session, "184656") == [
        "Year",
        "Make",
        "Model",
    ]


def test_compatibility_properties_unsupported_category_cached_empty(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(db, path, params):
        if path == "/get_default_category_tree_id":
            return {"categoryTreeId": "0"}
        return None  # eBay error → treated as "no fitment support"

    monkeypatch.setattr(ebay_taxonomy, "_request_json", fake_request)
    assert ebay_taxonomy.get_compatibility_properties(db_session, "11700") == []
    # Cached-empty: second lookup must not hit the API
    monkeypatch.setattr(
        ebay_taxonomy,
        "_request_json",
        lambda db, path, params: (_ for _ in ()).throw(AssertionError("no API call")),
    )
    assert ebay_taxonomy.get_compatibility_properties(db_session, "11700") == []


def test_request_json_returns_none_on_json_parse_error(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that malformed JSON in a 200 response is handled gracefully."""

    def fake_client_context():
        response = Mock()
        response.status_code = 200
        response.json.side_effect = ValueError("Expecting value")
        client = Mock()
        client.get.return_value = response
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        return client

    import httpx

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda: fake_client_context(),
    )

    # Monkeypatch get_ebay_token to avoid DB calls
    monkeypatch.setattr(ebay_taxonomy, "get_ebay_token", lambda db: "test_token")

    result = ebay_taxonomy._request_json(db_session, "/test_path", {"test": "param"})
    assert result is None

    # Verify the API quota was logged despite the error
    from app.db.models import ApiQuotaLog

    log_entry = (
        db_session.query(ApiQuotaLog)
        .filter(ApiQuotaLog.provider == "ebay_taxonomy")
        .order_by(ApiQuotaLog.id.desc())
        .first()
    )
    assert log_entry is not None
    assert log_entry.status_code == 200
