"""Tests for the deeper fetch limit on OEM-only searches.

An OEM-only search discards most of what eBay returns (the title filter
runs before persist), so the default 50-listing page starves it — the
2026-09-14 alternator search kept 6 of 50 while 125 listings matched.
eBay allows limit=200 in a single call (same quota cost), so OEM-only
searches with an OEM number fetch deep; everything else keeps the
default, where deeper pages are unfiltered noise.
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.core import search_runner
from app.db.models import Search
from app.sources.ebay_browse import FitmentFilterError, NormalizedListing


def make_listing(item_id: str = "v1|1|0") -> NormalizedListing:
    return NormalizedListing(
        ebay_item_id=item_id,
        title="Crossover Pipe OEM LR010819",
        price=Decimal("89.99"),
        currency="USD",
        condition="New",
        seller_name="seller",
        seller_feedback_score=100,
        seller_feedback_pct=Decimal("99.5"),
        item_url="https://www.ebay.com/itm/1",
        image_url=None,
        ebay_end_date=None,
        category_id="177697",
        compatibility_match="EXACT",
    )


class CapturingSearch:
    """Monkeypatch target capturing search_ebay calls."""

    def __init__(self, raise_first: bool = False):
        self.calls: list[dict] = []
        self.raise_first = raise_first

    def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        if self.raise_first and len(self.calls) == 1:
            raise FitmentFilterError("category rejected")
        return [make_listing()]


def test_oem_only_search_fetches_deep_in_fitment_mode(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_search.category_id = "177697"
    test_search.oem_only = True  # oem_number comes from the fixture
    db_session.commit()
    fake = CapturingSearch()
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    search_runner.run_single_search(db_session, test_search)

    assert fake.calls[0]["limit"] == settings.fetch_oem_deep_limit


def test_oem_only_search_fetches_deep_in_fallback_mode(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_search.category_id = None
    test_search.oem_only = True
    db_session.commit()
    fake = CapturingSearch()
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    search_runner.run_single_search(db_session, test_search)

    assert fake.calls[0]["limit"] == settings.fetch_oem_deep_limit


def test_oem_only_fitment_rejection_retry_stays_deep(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_search.category_id = "177697"
    test_search.oem_only = True
    db_session.commit()
    fake = CapturingSearch(raise_first=True)
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    search_runner.run_single_search(db_session, test_search)

    assert len(fake.calls) == 2
    assert fake.calls[1]["limit"] == settings.fetch_oem_deep_limit


def test_non_oem_search_keeps_default_limit(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No OEM filter → deeper pages are unfiltered noise; keep the default."""
    test_search.category_id = "177697"
    test_search.oem_only = False
    db_session.commit()
    fake = CapturingSearch()
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    search_runner.run_single_search(db_session, test_search)

    assert fake.calls[0]["limit"] is None


def test_oem_toggle_without_number_keeps_default_limit(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    """oem_only without a number has no filter to prune the deep page."""
    test_search.category_id = "177697"
    test_search.oem_only = True
    test_search.oem_number = None
    db_session.commit()
    fake = CapturingSearch()
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    search_runner.run_single_search(db_session, test_search)

    assert fake.calls[0]["limit"] is None
