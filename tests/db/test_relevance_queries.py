"""Tests for relevance query helpers."""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import Listing, Search, SearchListing


def link_listing(
    db: Session, search: Search, title: str = "Part", active: bool = True
) -> Listing:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title=title,
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
        is_active=active,
    )
    db.add(listing)
    db.flush()
    db.add(SearchListing(search_id=search.id, listing_id=listing.id))
    db.flush()
    return listing


def test_unclassified_links_active_only_with_limit(
    db_session: Session, test_search: Search
) -> None:
    active = link_listing(db_session, test_search)
    link_listing(db_session, test_search, active=False)
    links = queries.get_unclassified_links(db_session, test_search.id, limit=10)
    assert [link.listing_id for link in links] == [active.id]
    assert queries.get_unclassified_links(db_session, test_search.id, limit=0) == []


def test_set_and_map_relevance(db_session: Session, test_search: Search) -> None:
    listing = link_listing(db_session, test_search)
    queries.set_link_relevance(db_session, test_search.id, listing.id, "accessory")
    assert queries.get_unclassified_links(db_session, test_search.id, limit=10) == []
    mapping = queries.get_relevance_map(db_session, test_search.id)
    assert mapping == {listing.id: "accessory"}
    link = db_session.get(SearchListing, (test_search.id, listing.id))
    assert link is not None and link.relevance_checked_at is not None
