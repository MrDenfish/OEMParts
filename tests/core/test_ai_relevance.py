"""Tests for the AI relevance client. The Anthropic client is fully mocked
via the ai_relevance._client seam — no network, no API key needed."""

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.core import ai_relevance
from app.db.models import Listing, Search, Vehicle


def make_search(**kw) -> Search:
    defaults = dict(
        query_text="AMK air suspension compressor",
        oem_number="LR072537",
        category_name="Self-Leveling Suspension Parts",
    )
    defaults.update(kw)
    return Search(**defaults)  # type: ignore[arg-type]


def make_listing(title: str, price: str) -> Listing:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title=title,
        price=Decimal(price),
        item_url="https://www.ebay.com/itm/1",
    )
    listing.id = uuid.uuid4()
    return listing


def fake_response(payload: dict, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


@pytest.fixture(autouse=True)
def _ai_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ai_filter_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "ai_model", "claude-opus-5")


def patch_client(monkeypatch: pytest.MonkeyPatch, response) -> MagicMock:
    client = MagicMock()
    if isinstance(response, Exception):
        client.messages.create.side_effect = response
    else:
        client.messages.create.return_value = response
    monkeypatch.setattr(ai_relevance, "_client", lambda: client)
    return client


class TestClassifyListings:
    def test_maps_verdicts_by_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        search = make_search()
        listings = [
            make_listing("AMK compressor unit", "230.65"),
            make_listing("Bracket mount kit", "24.99"),
        ]
        client = patch_client(
            monkeypatch,
            fake_response(
                {
                    "verdicts": [
                        {"index": 0, "verdict": "part"},
                        {"index": 1, "verdict": "accessory"},
                    ]
                }
            ),
        )
        result = ai_relevance.classify_listings(search, listings)
        assert result == {
            listings[0].id: "part",
            listings[1].id: "accessory",
        }
        assert client.messages.create.call_count == 1
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-5"
        prompt = kwargs["messages"][0]["content"]
        assert "AMK compressor unit" in prompt and "LR072537" in prompt

    def test_empty_input_returns_empty_without_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = patch_client(monkeypatch, fake_response({"verdicts": []}))
        assert ai_relevance.classify_listings(make_search(), []) == {}
        assert client.messages.create.call_count == 0

    def test_api_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        patch_client(
            monkeypatch,
            anthropic.APIConnectionError(request=MagicMock()),
        )
        result = ai_relevance.classify_listings(
            make_search(), [make_listing("x", "1.00")]
        )
        assert result is None

    def test_refusal_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_client(monkeypatch, fake_response({}, stop_reason="refusal"))
        assert (
            ai_relevance.classify_listings(make_search(), [make_listing("x", "1.00")])
            is None
        )

    def test_invalid_verdict_and_index_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listings = [make_listing("a", "1.00"), make_listing("b", "2.00")]
        patch_client(
            monkeypatch,
            fake_response(
                {
                    "verdicts": [
                        {"index": 0, "verdict": "sideways"},  # bad verdict → skipped
                        {"index": 9, "verdict": "part"},  # bad index → skipped
                        {"index": 1, "verdict": "unrelated"},
                    ]
                }
            ),
        )
        result = ai_relevance.classify_listings(make_search(), listings)
        assert result == {listings[1].id: "unrelated"}

    def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="not json {")],
        )
        patch_client(monkeypatch, response)
        assert (
            ai_relevance.classify_listings(make_search(), [make_listing("x", "1.00")])
            is None
        )

    def test_non_object_json_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(["oops"]))],
        )
        patch_client(monkeypatch, response)
        assert (
            ai_relevance.classify_listings(make_search(), [make_listing("x", "1.00")])
            is None
        )


class TestCraftQuery:
    def _vehicle(self) -> Vehicle:
        return Vehicle(year=2012, make="Land Rover", model="LR4")  # type: ignore[call-arg]

    def test_returns_refined_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_client(
            monkeypatch, fake_response({"query": "AMK air suspension compressor"})
        )
        result = ai_relevance.craft_query(
            "Waterpump to Thermostat", "LR072537", self._vehicle(), None
        )
        assert result == "AMK air suspension compressor"

    def test_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        patch_client(monkeypatch, anthropic.APIConnectionError(request=MagicMock()))
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None

    def test_blank_or_oversized_result_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_client(monkeypatch, fake_response({"query": "   "}))
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None
        patch_client(monkeypatch, fake_response({"query": "y" * 500}))
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None

    def test_refusal_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_client(monkeypatch, fake_response({}, stop_reason="refusal"))
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None

    def test_malformed_json_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="not json {")],
        )
        patch_client(monkeypatch, response)
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None
