"""UI tests: price badges on listings, median column on searches."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Listing, PriceHistory, Search, SearchListing, User, utcnow


@pytest.fixture(autouse=True)
def _force_basic_auth(monkeypatch: pytest.MonkeyPatch):
    """Keep these tests hermetic regardless of the developer's .env."""
    monkeypatch.setattr(settings, "auth_backend", "basic")


@pytest.fixture()
def authed_client(db_session: Session, test_user: User) -> TestClient:
    """Authenticated test client scoped to test_user's own data.

    The plain `client` fixture authenticates as the auto-created "admin"
    basic-auth user, a different account from `test_user`. Since listings
    and searches are strictly user_id-scoped (multi-tenancy contract),
    `client` can never see `test_search`'s rows. Overriding
    `get_current_user` to return `test_user` directly is the established
    pattern for tests that assert on a specific user's page content — see
    tests/web/test_search_create_category.py.
    """
    from app.auth.dependencies import get_current_user
    from app.db.session import get_db
    from app.web.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_get_current_user():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    test_client = TestClient(app)

    yield test_client

    app.dependency_overrides.clear()


def seed(db: Session, search: Search) -> Listing:
    """Five listings incl. one with a recorded price drop; returns the dropper."""
    prices = ["25.00", "110.00", "120.00", "130.00", "140.00"]
    listings = []
    for p in prices:
        listing = Listing(
            ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
            title=f"Part at {p}",
            price=Decimal(p),
            item_url="https://www.ebay.com/itm/1",
        )
        db.add(listing)
        db.flush()
        db.add(SearchListing(search_id=search.id, listing_id=listing.id))
        listings.append(listing)
    dropper = listings[0]  # 25.00 — also the cheapest (low badge)
    db.add(
        PriceHistory(
            listing_id=dropper.id,
            price=Decimal("50.00"),
            recorded_at=utcnow() - timedelta(days=2),
        )
    )
    db.add(
        PriceHistory(
            listing_id=dropper.id,
            price=Decimal("25.00"),
            recorded_at=utcnow() - timedelta(hours=1),
        )
    )
    db.commit()
    return dropper


def test_listing_badges_render(
    authed_client: TestClient, db_session: Session, test_search: Search
) -> None:
    seed(db_session, test_search)
    page = authed_client.get(f"/listings/?search_id={test_search.id}")
    assert page.status_code == 200
    assert "▼ 50%" in page.text  # drop badge
    assert "cheapest in this search" in page.text  # low badge tooltip


def test_searches_median_column(
    authed_client: TestClient, db_session: Session, test_search: Search
) -> None:
    seed(db_session, test_search)
    page = authed_client.get("/searches/")
    assert page.status_code == 200
    assert "<th>Median $</th>" in page.text
    assert "$120.00" in page.text


def test_median_dash_below_minimum(
    authed_client: TestClient, test_search: Search
) -> None:
    page = authed_client.get("/searches/")
    assert page.status_code == 200
    assert "<th>Median $</th>" in page.text  # column exists even with no stats
