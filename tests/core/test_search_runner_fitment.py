"""Tests for search_runner's fitment vs fallback fetch modes."""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core import search_runner
from app.db.models import Listing, Search
from app.sources.ebay_browse import FitmentFilterError, NormalizedListing


def make_listing(item_id: str = "v1|1|0") -> NormalizedListing:
    return NormalizedListing(
        ebay_item_id=item_id,
        title="Water Pump OEM LR010819",
        price=Decimal("89.99"),
        currency="USD",
        condition="New",
        seller_name="seller",
        seller_feedback_score=100,
        seller_feedback_pct=Decimal("99.5"),
        item_url="https://www.ebay.com/itm/1",
        image_url=None,
        ebay_end_date=None,
        category_id="184656",
        compatibility_match="EXACT",
    )


class CapturingSearch:
    """Monkeypatch target capturing search_ebay calls."""

    def __init__(self, results=None, raise_first=False):
        self.calls: list[dict] = []
        self.results = results if results is not None else [make_listing()]
        self.raise_first = raise_first

    def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        if self.raise_first and len(self.calls) == 1:
            raise FitmentFilterError("category rejected")
        return self.results


def test_fitment_mode_sends_bare_query_and_category(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_search.category_id = "184656"
    test_search.category_name = "Water Pumps"
    db_session.commit()
    fake = CapturingSearch()
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    result = search_runner.run_single_search(db_session, test_search)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["query"] == "LR4 coolant crossover pipe"  # bare — no vehicle prefix
    assert call["category_ids"] == "184656"
    assert call["compatibility_filter"] == "Year:2012;Make:Land Rover;Model:LR4"
    assert result.api_calls_made == 1


def test_fallback_mode_sends_enriched_query(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert test_search.category_id is None
    fake = CapturingSearch()
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    search_runner.run_single_search(db_session, test_search)

    call = fake.calls[0]
    assert call["query"] == "2012 Land Rover LR4 LR4 coolant crossover pipe"
    assert "category_ids" not in call
    assert "compatibility_filter" not in call


def test_fitment_error_falls_back_once(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_search.category_id = "184656"
    db_session.commit()
    fake = CapturingSearch(raise_first=True)
    monkeypatch.setattr(search_runner, "search_ebay", fake)

    result = search_runner.run_single_search(db_session, test_search)

    assert len(fake.calls) == 2
    assert fake.calls[1]["query"] == "2012 Land Rover LR4 LR4 coolant crossover pipe"
    assert "compatibility_filter" not in fake.calls[1]
    assert result.api_calls_made == 2
    # Category stays in place — transient eBay problems shouldn't erase it
    db_session.refresh(test_search)
    assert test_search.category_id == "184656"


def test_fitment_mode_sets_compatibility_checked(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_search.category_id = "184656"
    db_session.commit()
    monkeypatch.setattr(search_runner, "search_ebay", CapturingSearch())

    search_runner.run_single_search(db_session, test_search)

    listing = db_session.query(Listing).filter_by(ebay_item_id="v1|1|0").one()
    assert listing.compatibility_checked is True


def test_fallback_mode_does_not_set_compatibility_checked(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_runner, "search_ebay", CapturingSearch())

    search_runner.run_single_search(db_session, test_search)

    listing = db_session.query(Listing).filter_by(ebay_item_id="v1|1|0").one()
    assert listing.compatibility_checked is False


def test_update_never_downgrades_compatibility_checked(
    db_session: Session, test_search: Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First fetch in fitment mode → checked True
    test_search.category_id = "184656"
    db_session.commit()
    monkeypatch.setattr(search_runner, "search_ebay", CapturingSearch())
    search_runner.run_single_search(db_session, test_search)

    # Second fetch of the same listing in fallback mode → stays True
    test_search.category_id = None
    db_session.commit()
    search_runner.run_single_search(db_session, test_search)

    listing = db_session.query(Listing).filter_by(ebay_item_id="v1|1|0").one()
    assert listing.compatibility_checked is True
