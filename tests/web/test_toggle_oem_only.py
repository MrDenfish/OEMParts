"""Route tests for the OEM-only toggle's self-healing behavior.

Toggling OEM-only back ON must re-apply the title filter to listings that
were linked while the filter was off (the filter is otherwise fetch-time
only, so stale links would persist on the dashboard indefinitely).
"""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Listing, Search, SearchListing


@pytest.fixture(autouse=True)
def _force_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the basic-auth backend for every test in this module.

    Same pattern as tests/web/test_routes.py: the settings singleton reads
    the developer's real .env, so AUTH_BACKEND=clerk there would otherwise
    send these requests down the Clerk path and break the assertions.

    Also pins the basic-auth username to the test_user fixture's email:
    in basic mode get_current_user resolves the User by that username, and
    these tests mutate test_user's search — a different username would be
    a different tenant and the routes would correctly 404.
    """
    monkeypatch.setattr(settings, "auth_backend", "basic")
    monkeypatch.setattr(settings, "basic_auth_username", "testuser@example.com")


def _link_listing(db: Session, search: Search, title: str) -> Listing:
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


def _link_count(db: Session, search: Search) -> int:
    return db.query(SearchListing).filter(SearchListing.search_id == search.id).count()


def test_toggle_on_prunes_non_matching_links(
    client: TestClient, db_session: Session, test_search: Search
) -> None:
    """Turning OEM-only ON removes links whose titles fail the filter."""
    assert test_search.oem_only is False  # model default; toggle lands ON
    matching = _link_listing(db_session, test_search, "Genuine Crossover Pipe LR010819")
    _link_listing(
        db_session, test_search, "Heavy Duty Crossover Pipe fits LR4 2010-2012"
    )
    db_session.commit()

    response = client.patch(f"/searches/{test_search.id}/toggle-oem-only")

    assert response.status_code == 200
    db_session.refresh(test_search)
    assert test_search.oem_only is True
    links = (
        db_session.query(SearchListing)
        .filter(SearchListing.search_id == test_search.id)
        .all()
    )
    assert [link.listing_id for link in links] == [matching.id]


def test_toggle_off_leaves_links_untouched(
    client: TestClient, db_session: Session, test_search: Search
) -> None:
    """Turning OEM-only OFF never removes anything."""
    test_search.oem_only = True
    _link_listing(
        db_session, test_search, "Heavy Duty Crossover Pipe fits LR4 2010-2012"
    )
    db_session.commit()

    response = client.patch(f"/searches/{test_search.id}/toggle-oem-only")

    assert response.status_code == 200
    db_session.refresh(test_search)
    assert test_search.oem_only is False
    assert _link_count(db_session, test_search) == 1
