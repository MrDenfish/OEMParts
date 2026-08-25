"""Schema tests for listing aspects columns."""

import uuid
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Listing, utcnow


def test_aspects_default_to_none(db_session: Session) -> None:
    """Verify all aspect columns default to NULL."""
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Test Part",
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    assert listing.brand is None
    assert listing.mpn is None
    assert listing.oe_part_number is None
    assert listing.aspects_fetched_at is None


def test_aspects_roundtrip(db_session: Session) -> None:
    """Verify aspect columns can be set and retrieved correctly."""
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="OEM Part",
        price=Decimal("25.00"),
        item_url="https://www.ebay.com/itm/2",
        brand="Toyota",
        mpn="12345-67890",
        oe_part_number="OEM-98765",
        aspects_fetched_at=utcnow(),
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    assert listing.brand == "Toyota"
    assert listing.mpn == "12345-67890"
    assert listing.oe_part_number == "OEM-98765"
    assert listing.aspects_fetched_at is not None
    assert isinstance(listing.aspects_fetched_at, datetime)


def test_aspects_timezone_aware(db_session: Session) -> None:
    """Verify aspects_fetched_at maintains UTC timezone awareness."""
    fetch_time = utcnow()
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Test Part",
        price=Decimal("15.00"),
        item_url="https://www.ebay.com/itm/3",
        aspects_fetched_at=fetch_time,
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    assert listing.aspects_fetched_at is not None
    assert listing.aspects_fetched_at.tzinfo is not None
    # Verify it's UTC
    assert listing.aspects_fetched_at.tzinfo == timezone.utc


def test_aspects_partial_set(db_session: Session) -> None:
    """Verify that only some aspects can be set while others remain NULL."""
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Test Part",
        price=Decimal("20.00"),
        item_url="https://www.ebay.com/itm/4",
        brand="Honda",
        mpn=None,
        oe_part_number="OEM-11111",
        aspects_fetched_at=utcnow(),
    )
    db_session.add(listing)
    db_session.commit()
    db_session.refresh(listing)

    assert listing.brand == "Honda"
    assert listing.mpn is None
    assert listing.oe_part_number == "OEM-11111"
    assert listing.aspects_fetched_at is not None
