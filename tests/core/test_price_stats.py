"""Tests for price statistics and deal signals."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.core.price_stats import (
    MIN_LISTINGS_FOR_STATS,
    _nearest_rank,
    is_low_in_search,
    recent_drop,
    search_price_stats,
)
from app.db.models import Listing, PriceHistory, Search, SearchListing, utcnow


def make_listing(db: Session, price: str, item_id: str | None = None) -> Listing:
    listing = Listing(
        ebay_item_id=item_id or f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Test part",
        price=Decimal(price),
        item_url="https://www.ebay.com/itm/1",
    )
    db.add(listing)
    db.flush()
    return listing


def snapshot(db: Session, listing: Listing, price: str, days_ago: float) -> None:
    db.add(
        PriceHistory(
            listing_id=listing.id,
            price=Decimal(price),
            recorded_at=utcnow() - timedelta(days=days_ago),
        )
    )
    db.flush()


def link(db: Session, search: Search, listing: Listing) -> None:
    db.add(SearchListing(search_id=search.id, listing_id=listing.id))
    db.flush()


class TestRecentDrop:
    def test_drop_vs_window_peak(self, db_session: Session) -> None:
        listing = make_listing(db_session, "48.00")
        snapshot(db_session, listing, "77.71", days_ago=3)
        snapshot(db_session, listing, "77.71", days_ago=2)
        snapshot(db_session, listing, "48.00", days_ago=0.1)
        drop = recent_drop(db_session, listing.id, lookback_days=7)
        assert drop is not None
        assert drop.old_price == Decimal("77.71")
        assert drop.new_price == Decimal("48.00")
        assert drop.pct == Decimal("38")  # rounded to whole percent

    def test_below_threshold_is_not_a_drop(self, db_session: Session) -> None:
        listing = make_listing(db_session, "95.00")
        snapshot(db_session, listing, "100.00", days_ago=1)
        snapshot(db_session, listing, "95.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None  # 5% < 10%

    def test_exactly_threshold_is_a_drop(self, db_session: Session) -> None:
        listing = make_listing(db_session, "90.00")
        snapshot(db_session, listing, "100.00", days_ago=1)
        snapshot(db_session, listing, "90.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is not None

    def test_peak_outside_lookback_ignored(self, db_session: Session) -> None:
        listing = make_listing(db_session, "48.00")
        snapshot(db_session, listing, "77.71", days_ago=10)  # outside 7-day window
        snapshot(db_session, listing, "48.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None

    def test_single_snapshot_is_none(self, db_session: Session) -> None:
        listing = make_listing(db_session, "48.00")
        snapshot(db_session, listing, "48.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None

    def test_price_rise_is_none(self, db_session: Session) -> None:
        listing = make_listing(db_session, "100.00")
        snapshot(db_session, listing, "80.00", days_ago=1)
        snapshot(db_session, listing, "100.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None

    def test_unrounded_drop_below_threshold_is_none(self, db_session: Session) -> None:
        # 9.6% raw drop would quantize (ROUND_HALF_UP) to 10% and incorrectly
        # pass a 10% threshold if compared post-rounding. Must compare raw.
        assert settings.digest_price_drop_pct == 10
        listing = make_listing(db_session, "90.40")
        snapshot(db_session, listing, "100.00", days_ago=1)
        snapshot(db_session, listing, "90.40", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None


class TestSearchPriceStats:
    def test_stats_computed(self, db_session: Session, test_search: Search) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]:
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        stats = search_price_stats(db_session, test_search.id)
        assert stats is not None
        assert stats.count == 5
        assert stats.minimum == Decimal("25.00")
        assert stats.median == Decimal("100.00")  # nearest-rank 50th of 5
        assert stats.p25 == Decimal("30.00")  # nearest-rank 25th of 5

    def test_below_minimum_count_is_none(
        self, db_session: Session, test_search: Search
    ) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00"]:  # only 4
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        assert MIN_LISTINGS_FOR_STATS == 5
        assert search_price_stats(db_session, test_search.id) is None

    def test_inactive_listings_excluded(
        self, db_session: Session, test_search: Search
    ) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]:
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        dead = make_listing(db_session, "1.00")
        dead.is_active = False
        link(db_session, test_search, dead)
        db_session.flush()
        stats = search_price_stats(db_session, test_search.id)
        assert stats is not None
        assert stats.count == 5
        assert stats.minimum == Decimal("25.00")


class TestNearestRank:
    def test_integer_math_matches_percentile_20_of_5(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # percentile=20, n=5 -> ceil(20*5/100) - 1 = ceil(1.0) - 1 = 0 ->
        # the 1st (cheapest) element, exercising the integer ceil-division
        # boundary (exact multiple of 100) rather than the usual 25% case.
        monkeypatch.setattr(settings, "deal_percentile", 20)
        prices = [Decimal(p) for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]]
        assert _nearest_rank(prices, settings.deal_percentile) == Decimal("25.00")


class TestIsLowInSearch:
    def test_low_and_not_low(self, db_session: Session, test_search: Search) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]:
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        stats = search_price_stats(db_session, test_search.id)
        assert is_low_in_search(Decimal("28.00"), stats) is True
        assert is_low_in_search(Decimal("30.00"), stats) is True  # == p25
        assert is_low_in_search(Decimal("31.00"), stats) is False
        assert is_low_in_search(Decimal("28.00"), None) is False
