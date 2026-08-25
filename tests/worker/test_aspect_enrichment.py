"""Tests for fetch-cycle Item Specifics enrichment."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FetchRun, Listing, utcnow
from app.sources.ebay_item import ItemAspects
from app.worker import fetcher


def _make_listing(db_session: Session, aspects_fetched_at=None) -> Listing:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Test Part",
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
        aspects_fetched_at=aspects_fetched_at,
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)
    return listing


def test_pending_listings_get_persisted_and_stamped(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    listing1 = _make_listing(db_session)
    listing2 = _make_listing(db_session)

    monkeypatch.setattr(
        fetcher,
        "fetch_item_aspects",
        lambda db, ebay_item_id: ItemAspects(
            brand="Dorman", mpn="949-919", oe_part_number="LR124471"
        ),
    )

    count = fetcher.enrich_listing_aspects(db_session)

    assert count == 2
    db_session.refresh(listing1)
    db_session.refresh(listing2)
    for listing in (listing1, listing2):
        assert listing.brand == "Dorman"
        assert listing.mpn == "949-919"
        assert listing.oe_part_number == "LR124471"
        assert listing.aspects_fetched_at is not None


def test_transient_failure_leaves_fields_null_for_retry(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    listing = _make_listing(db_session)

    monkeypatch.setattr(fetcher, "fetch_item_aspects", lambda db, ebay_item_id: None)

    count = fetcher.enrich_listing_aspects(db_session)

    assert count == 0
    db_session.refresh(listing)
    assert listing.brand is None
    assert listing.mpn is None
    assert listing.oe_part_number is None
    assert listing.aspects_fetched_at is None


def test_all_none_aspects_still_stamped_never_retried(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    listing = _make_listing(db_session)

    monkeypatch.setattr(
        fetcher,
        "fetch_item_aspects",
        lambda db, ebay_item_id: ItemAspects(brand=None, mpn=None, oe_part_number=None),
    )

    count = fetcher.enrich_listing_aspects(db_session)

    assert count == 1
    db_session.refresh(listing)
    assert listing.brand is None
    assert listing.aspects_fetched_at is not None


def test_cap_limits_calls(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    for _ in range(3):
        _make_listing(db_session)

    calls = []

    def _fake_fetch(db, ebay_item_id):
        calls.append(ebay_item_id)
        return ItemAspects(brand="Dorman", mpn=None, oe_part_number=None)

    monkeypatch.setattr(fetcher, "fetch_item_aspects", _fake_fetch)

    count = fetcher.enrich_listing_aspects(db_session, cap=2)

    assert count == 2
    assert len(calls) == 2


def test_already_stamped_listing_skipped(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    _make_listing(db_session, aspects_fetched_at=utcnow())

    calls = []

    def _fake_fetch(db, ebay_item_id):
        calls.append(ebay_item_id)
        return ItemAspects(brand="Dorman", mpn=None, oe_part_number=None)

    monkeypatch.setattr(fetcher, "fetch_item_aspects", _fake_fetch)

    count = fetcher.enrich_listing_aspects(db_session)

    assert count == 0
    assert calls == []


def test_poison_listing_does_not_starve_the_queue(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """One listing whose fetch_item_aspects call raises must not stop the
    others behind it in the oldest-first queue from being enriched."""
    poison = _make_listing(db_session)
    healthy = _make_listing(db_session)

    def _fake_fetch(db, ebay_item_id):
        if ebay_item_id == poison.ebay_item_id:
            raise ValueError("malformed response shape")
        return ItemAspects(brand="Dorman", mpn=None, oe_part_number=None)

    monkeypatch.setattr(fetcher, "fetch_item_aspects", _fake_fetch)

    count = fetcher.enrich_listing_aspects(db_session)

    assert count == 1
    db_session.refresh(poison)
    db_session.refresh(healthy)
    assert poison.aspects_fetched_at is None
    assert healthy.aspects_fetched_at is not None
    assert healthy.brand == "Dorman"


def test_enrichment_failure_does_not_abort_the_fetch_cycle(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """An exception during enrichment must not leave the fetch_runs row dangling."""
    _make_listing(db_session)

    def _raise(db, ebay_item_id):
        raise RuntimeError("OAuth refresh blew up")

    monkeypatch.setattr(fetcher, "fetch_item_aspects", _raise)

    fetcher.run_fetch_cycle(db_session, cycle_type="nightly")

    runs = list(db_session.execute(select(FetchRun)).scalars())
    assert len(runs) == 1
    assert runs[0].completed_at is not None
