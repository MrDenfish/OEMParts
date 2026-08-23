"""Search creation resolves and stores a fitment category (or None)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.core.compatibility import ResolvedCategory
from app.core.search_runner import SearchResult
from app.db.models import Search, User, Vehicle
from app.web.routes import searches as searches_module


@pytest.fixture(autouse=True)
def _force_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the basic-auth backend for every test in this module.

    ``settings`` is a module-level singleton that loads ``AUTH_BACKEND`` from
    the developer's ``.env``. Without this, running the suite while ``.env`` has
    ``AUTH_BACKEND=clerk`` (e.g. mid-Clerk-testing) makes these tests exercise
    the Clerk path: the ``client`` fixture's HTTP Basic credentials don't
    authenticate, so the 401 becomes a 302 redirect to ``/sign-in`` and the
    assertions in ``_create`` break. The backend is read at request time, so
    patching the singleton is enough. See ``tests/auth/test_basic.py`` for
    the same pattern.
    """
    monkeypatch.setattr(settings, "auth_backend", "basic")


@pytest.fixture()
def client(db_session: Session, test_user: User) -> TestClient:
    """Provide an authenticated test client as the test_user.

    Overrides the global client fixture to use get_current_user directly,
    bypassing basic auth verification, so the test_user is always logged in.
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


@pytest.fixture(autouse=True)
def _no_auto_fetch(monkeypatch: pytest.MonkeyPatch):
    """Auto-fetch must not hit the network in tests."""
    monkeypatch.setattr(
        searches_module,
        "run_single_search",
        lambda db, search: SearchResult(
            search_id=str(search.id),
            listings_fetched=0,
            listings_new=0,
            listings_updated=0,
            api_calls_made=0,
            errors=0,
        ),
    )


def _create(client: TestClient, vehicle: Vehicle) -> None:
    response = client.post(
        "/searches/",
        data={"vehicle_id": str(vehicle.id), "query_text": "water pump"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303)


def test_create_stores_resolved_category(
    client: TestClient,
    db_session: Session,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        searches_module,
        "resolve_fitment_category",
        lambda db, q: ResolvedCategory("184656", "Water Pumps"),
    )
    _create(client, test_vehicle)
    search = db_session.query(Search).filter_by(query_text="water pump").one()
    assert search.category_id == "184656"
    assert search.category_name == "Water Pumps"


def test_create_survives_resolution_returning_none(
    client: TestClient,
    db_session: Session,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(searches_module, "resolve_fitment_category", lambda db, q: None)
    _create(client, test_vehicle)
    search = db_session.query(Search).filter_by(query_text="water pump").one()
    assert search.category_id is None
    assert search.category_name is None


def test_create_survives_resolution_raising(
    client: TestClient,
    db_session: Session,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(db, q):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(searches_module, "resolve_fitment_category", boom)
    _create(client, test_vehicle)  # must still succeed
    search = db_session.query(Search).filter_by(query_text="water pump").one()
    assert search.category_id is None


def test_searches_page_shows_category_column(
    client: TestClient,
    db_session: Session,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        searches_module,
        "resolve_fitment_category",
        lambda db, q: ResolvedCategory("184656", "Water Pumps"),
    )
    _create(client, test_vehicle)
    page = client.get("/searches/")
    assert page.status_code == 200
    assert "Water Pumps" in page.text
    assert "<th>Category</th>" in page.text
