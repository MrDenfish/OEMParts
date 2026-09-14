"""Tests for the Browse API client's fitment-mode request shape."""

import pytest
from sqlalchemy.orm import Session

from app.sources import ebay_browse
from app.sources.ebay_browse import FitmentFilterError, search_ebay


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """Captures the request; returns a canned response."""

    response: FakeResponse = FakeResponse(200, {"itemSummaries": []})
    captured: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, headers=None, timeout=None):
        FakeClient.captured = {"url": url, "params": params, "headers": headers}
        return FakeClient.response


ITEM = {
    "itemId": "v1|123|0",
    "title": "Water Pump for Land Rover LR4",
    "price": {"value": "89.99", "currency": "USD"},
    "itemWebUrl": "https://www.ebay.com/itm/123",
    "categories": [{"categoryId": "184656"}],
    "compatibilityMatch": "EXACT",
}


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ebay_browse.httpx, "Client", FakeClient)
    monkeypatch.setattr(ebay_browse, "get_ebay_token", lambda db: "test-token")
    FakeClient.response = FakeResponse(200, {"itemSummaries": []})
    FakeClient.captured = {}


def test_category_ids_passed_through(db_session: Session) -> None:
    search_ebay(
        db_session,
        query="water pump",
        compatibility_filter="Year:2012;Make:Land Rover;Model:LR4",
        category_ids="184656",
    )
    params = FakeClient.captured["params"]
    assert params["category_ids"] == "184656"
    assert params["compatibility_filter"] == "Year:2012;Make:Land Rover;Model:LR4"
    assert params["q"] == "water pump"


def test_no_category_ids_when_not_given(db_session: Session) -> None:
    search_ebay(db_session, query="water pump")
    assert "category_ids" not in FakeClient.captured["params"]
    assert "compatibility_filter" not in FakeClient.captured["params"]


def test_compatibility_match_parsed(db_session: Session) -> None:
    FakeClient.response = FakeResponse(200, {"itemSummaries": [ITEM]})
    results = search_ebay(
        db_session,
        query="water pump",
        compatibility_filter="Year:2012;Make:Land Rover;Model:LR4",
        category_ids="184656",
    )
    assert len(results) == 1
    assert results[0].compatibility_match == "EXACT"


def test_non_200_with_filter_raises_fitment_error(db_session: Session) -> None:
    FakeClient.response = FakeResponse(
        400,
        {
            "errors": [
                {"errorId": 12506, "message": "category does not support fitment"}
            ]
        },
    )
    with pytest.raises(FitmentFilterError):
        search_ebay(
            db_session,
            query="water pump",
            compatibility_filter="Year:2012;Make:Land Rover;Model:LR4",
            category_ids="184656",
        )


def test_non_200_without_filter_returns_empty(db_session: Session) -> None:
    FakeClient.response = FakeResponse(500, {"errors": []})
    assert search_ebay(db_session, query="water pump") == []


def test_rate_limit_with_filter_returns_empty_without_raising(
    db_session: Session,
) -> None:
    """A 429 (or 5xx) must not trigger the fitment retry's second API call."""
    FakeClient.response = FakeResponse(
        429, {"errors": [{"errorId": 10001, "message": "rate limit exceeded"}]}
    )
    result = search_ebay(
        db_session,
        query="water pump",
        compatibility_filter="Year:2012;Make:Land Rover;Model:LR4",
        category_ids="184656",
    )
    assert result == []


def test_server_error_with_filter_returns_empty_without_raising(
    db_session: Session,
) -> None:
    FakeClient.response = FakeResponse(500, {"errors": []})
    result = search_ebay(
        db_session,
        query="water pump",
        compatibility_filter="Year:2012;Make:Land Rover;Model:LR4",
        category_ids="184656",
    )
    assert result == []


class MalformedJsonResponse:
    """A response whose .json() raises, mimicking a malformed body."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = "not json"

    def json(self) -> dict:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_200_with_malformed_json_returns_empty(db_session: Session) -> None:
    FakeClient.response = MalformedJsonResponse(200)
    assert search_ebay(db_session, query="water pump") == []


def test_non_200_with_malformed_json_and_filter_returns_empty(
    db_session: Session,
) -> None:
    """Malformed error body with compatibility_filter set must not raise."""
    FakeClient.response = MalformedJsonResponse(400)
    result = search_ebay(
        db_session,
        query="water pump",
        compatibility_filter="Year:2012;Make:Land Rover;Model:LR4",
        category_ids="184656",
    )
    assert result == []


def test_limit_override_passed_and_clamped_to_ebay_max(db_session: Session) -> None:
    """A caller's limit override is sent to eBay, clamped to the API max (200)."""
    ebay_browse.search_ebay(db_session, query="alternator", limit=200)
    assert FakeClient.captured["params"]["limit"] == 200

    ebay_browse.search_ebay(db_session, query="alternator", limit=500)
    assert FakeClient.captured["params"]["limit"] == 200
