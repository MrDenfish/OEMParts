"""Tests for database query helpers."""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import User, Vehicle


def test_create_and_get_vehicle(db_session: Session, test_user: User) -> None:
    """Create a vehicle and retrieve it."""
    vehicle = queries.create_vehicle(
        db_session,
        user_id=test_user.id,
        year=2012,
        make="Land Rover",
        model="LR4",
        nickname="My LR4",
    )
    db_session.commit()

    retrieved = queries.get_vehicle_by_id(db_session, vehicle.id, test_user.id)
    assert retrieved is not None
    assert retrieved.year == 2012
    assert retrieved.make == "Land Rover"
    assert retrieved.nickname == "My LR4"


def test_vehicle_user_scoping(db_session: Session, test_user: User) -> None:
    """User A cannot see User B's vehicles."""
    vehicle = queries.create_vehicle(
        db_session,
        user_id=test_user.id,
        year=2012,
        make="Land Rover",
        model="LR4",
    )
    db_session.commit()

    # Create a different user
    other_user = User(email="other@example.com")
    db_session.add(other_user)
    db_session.commit()

    # Other user should not see the vehicle
    result = queries.get_vehicle_by_id(db_session, vehicle.id, other_user.id)
    assert result is None

    other_vehicles = queries.get_vehicles_for_user(db_session, other_user.id)
    assert len(other_vehicles) == 0


def test_delete_vehicle(
    db_session: Session, test_user: User, test_vehicle: Vehicle
) -> None:
    """Delete a vehicle."""
    deleted = queries.delete_vehicle(db_session, test_vehicle.id, test_user.id)
    db_session.commit()
    assert deleted is True

    # Should be gone
    result = queries.get_vehicle_by_id(db_session, test_vehicle.id, test_user.id)
    assert result is None


def test_create_and_toggle_search(
    db_session: Session, test_user: User, test_vehicle: Vehicle
) -> None:
    """Create a search and toggle its active state."""
    search = queries.create_search(
        db_session,
        user_id=test_user.id,
        vehicle_id=test_vehicle.id,
        query_text="LR4 water pump",
        oem_number="LR033993",
    )
    db_session.commit()
    assert search.is_active is True

    toggled = queries.toggle_search_active(db_session, search.id, test_user.id)
    db_session.commit()
    assert toggled is not None
    assert toggled.is_active is False


def test_upsert_listing_insert_and_update(db_session: Session) -> None:
    """Test that upsert creates on first call and updates on second."""
    listing, is_new = queries.upsert_listing(
        db_session,
        ebay_item_id="v1|123456789|0",
        title="Test Part",
        price=Decimal("99.99"),
        currency="USD",
        item_url="https://www.ebay.com/itm/123456789",
    )
    db_session.commit()
    assert is_new is True
    assert listing.price == Decimal("99.99")

    # Second call with updated price
    listing2, is_new2 = queries.upsert_listing(
        db_session,
        ebay_item_id="v1|123456789|0",
        title="Test Part",
        price=Decimal("89.99"),
        currency="USD",
        item_url="https://www.ebay.com/itm/123456789",
    )
    db_session.commit()
    assert is_new2 is False
    assert listing2.price == Decimal("89.99")
    assert listing2.id == listing.id  # Same row


def test_price_history(db_session: Session) -> None:
    """Record price snapshots and retrieve them."""
    listing, _ = queries.upsert_listing(
        db_session,
        ebay_item_id="v1|999|0",
        title="Price Test Part",
        price=Decimal("50.00"),
        currency="USD",
        item_url="https://www.ebay.com/itm/999",
    )
    db_session.commit()

    queries.record_price_snapshot(db_session, listing.id, Decimal("50.00"))
    queries.record_price_snapshot(db_session, listing.id, Decimal("45.00"))
    db_session.commit()

    history = queries.get_price_history_for_listing(db_session, listing.id)
    assert len(history) == 2
    # Most recent first
    assert history[0].price == Decimal("45.00")
    assert history[1].price == Decimal("50.00")
