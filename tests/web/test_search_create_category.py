"""Search creation resolves and stores a fitment category (or None)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.compatibility import ResolvedCategory
from app.core.search_runner import SearchResult
from app.db.models import Search, Vehicle
from app.web.routes import searches as searches_module


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
