"""Tests for alert query helpers (digest dedup and retry semantics)."""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import Listing, Search, User, utcnow
from decimal import Decimal


def make_listing(db: Session) -> Listing:
    import uuid

    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Part",
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
    )
    db.add(listing)
    db.flush()
    return listing


def test_create_and_fetch_unnotified(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    listing = make_listing(db_session)
    alert = queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "price_drop"
    )
    assert alert.notified_at is None
    pending = queries.get_unnotified_alerts(db_session, test_user.id)
    assert [a.id for a in pending] == [alert.id]


def test_mark_notified_removes_from_pending(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    listing = make_listing(db_session)
    alert = queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "new_listing"
    )
    queries.mark_alerts_notified(db_session, [alert.id])
    assert queries.get_unnotified_alerts(db_session, test_user.id) == []
    db_session.refresh(alert)
    assert alert.notified_at is not None


def test_recent_alert_dedup_window(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    listing = make_listing(db_session)
    alert = queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "price_drop"
    )
    assert queries.recent_alert_exists(
        db_session, test_search.id, listing.id, "price_drop"
    )
    # Different type is independent
    assert not queries.recent_alert_exists(
        db_session, test_search.id, listing.id, "new_listing"
    )
    # Age the alert past the window
    alert.triggered_at = utcnow() - timedelta(days=8)
    db_session.flush()
    assert not queries.recent_alert_exists(
        db_session, test_search.id, listing.id, "price_drop"
    )


def test_unnotified_alerts_are_user_scoped(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    other = User(email="other@example.com")
    db_session.add(other)
    db_session.flush()
    listing = make_listing(db_session)
    queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "price_drop"
    )
    assert queries.get_unnotified_alerts(db_session, other.id) == []
