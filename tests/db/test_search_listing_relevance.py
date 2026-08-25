"""Schema tests for search_listings relevance columns."""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import Listing, Search, SearchListing, utcnow


def test_relevance_defaults_to_none(db_session: Session, test_search: Search) -> None:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Part",
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
    )
    db_session.add(listing)
    db_session.flush()
    link = SearchListing(search_id=test_search.id, listing_id=listing.id)
    db_session.add(link)
    db_session.commit()
    assert link.relevance is None
    assert link.relevance_checked_at is None


def test_relevance_roundtrip(db_session: Session, test_search: Search) -> None:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Bracket",
        price=Decimal("25.00"),
        item_url="https://www.ebay.com/itm/2",
    )
    db_session.add(listing)
    db_session.flush()
    link = SearchListing(
        search_id=test_search.id,
        listing_id=listing.id,
        relevance="accessory",
        relevance_checked_at=utcnow(),
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    assert link.relevance == "accessory"
    assert link.relevance_checked_at is not None
