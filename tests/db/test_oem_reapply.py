"""Tests for re-applying the OEM-only filter to already-linked listings.

The OEM-only filter runs at fetch time only, so listings linked while the
filter was toggled off survive when it is toggled back on (2026-09-14
incident: 47 aftermarket alternators linked during a brief toggle-off).
``remove_non_oem_links`` prunes those stale links.
"""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import Listing, Search, SearchListing


def _link_listing(db: Session, search: Search, title: str) -> Listing:
    """Create a listing with the given title and link it to the search."""
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title=title,
        price=Decimal("100.00"),
        item_url="https://www.ebay.com/itm/1",
    )
    db.add(listing)
    db.flush()
    db.add(SearchListing(search_id=search.id, listing_id=listing.id))
    db.flush()
    return listing


def test_removes_links_whose_titles_fail_oem_filter(
    db_session: Session, test_search: Search
) -> None:
    """Links failing title_matches_oem are deleted; passing ones survive."""
    # test_search has oem_number="LR010819"
    matching = _link_listing(
        db_session, test_search, "Coolant Crossover Pipe LR010819 for LR4"
    )
    genuine = _link_listing(
        db_session, test_search, "Genuine Land Rover Crossover Pipe"
    )
    aftermarket = _link_listing(
        db_session, test_search, "Heavy Duty Crossover Pipe fits LR4 2010-2012"
    )
    db_session.commit()

    removed = queries.remove_non_oem_links(db_session, test_search)
    db_session.commit()

    assert removed == 1
    remaining = {
        link.listing_id
        for link in db_session.query(SearchListing)
        .filter(SearchListing.search_id == test_search.id)
        .all()
    }
    assert remaining == {matching.id, genuine.id}
    # The listing row itself is shared data and must NOT be deleted.
    assert db_session.get(Listing, aftermarket.id) is not None


def test_returns_zero_when_all_links_match(
    db_session: Session, test_search: Search
) -> None:
    _link_listing(db_session, test_search, "OEM Crossover Pipe for LR4")
    _link_listing(db_session, test_search, "Water Pipe LR-010819 New")
    db_session.commit()

    removed = queries.remove_non_oem_links(db_session, test_search)

    assert removed == 0
    count = (
        db_session.query(SearchListing)
        .filter(SearchListing.search_id == test_search.id)
        .count()
    )
    assert count == 2


def test_removes_nothing_when_search_has_no_oem_number(
    db_session: Session, test_search: Search
) -> None:
    """Without an OEM number the filter has nothing to enforce."""
    test_search.oem_number = None
    _link_listing(db_session, test_search, "Heavy Duty Crossover Pipe fits LR4")
    db_session.commit()

    removed = queries.remove_non_oem_links(db_session, test_search)

    assert removed == 0
    count = (
        db_session.query(SearchListing)
        .filter(SearchListing.search_id == test_search.id)
        .count()
    )
    assert count == 1
