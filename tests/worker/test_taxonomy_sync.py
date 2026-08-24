"""Tests for the taxonomy-sync backfill job."""

import pytest
from sqlalchemy.orm import Session

from app.core.compatibility import ResolvedCategory
from app.db.models import Search, User, Vehicle
from app.worker import taxonomy_sync


def _search(db: Session, user: User, vehicle: Vehicle, query: str, **kw) -> Search:
    search = Search(
        user_id=user.id, vehicle_id=vehicle.id, query_text=query, is_active=True, **kw
    )
    db.add(search)
    db.commit()
    db.refresh(search)
    return search


def test_backfills_only_missing_categories(
    db_session: Session,
    test_user: User,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _search(db_session, test_user, test_vehicle, "water pump")
    already = _search(
        db_session,
        test_user,
        test_vehicle,
        "thermostat",
        category_id="1000",
        category_name="Existing",
    )
    resolved_queries: list[str] = []

    def fake_resolve(db, q):
        resolved_queries.append(q)
        return ResolvedCategory("184656", "Water Pumps")

    monkeypatch.setattr(taxonomy_sync, "resolve_fitment_category", fake_resolve)
    results = taxonomy_sync.run_taxonomy_sync(db_session, resolve_all=False)

    assert resolved_queries == ["water pump"]
    assert len(results) == 1
    db_session.refresh(missing)
    db_session.refresh(already)
    assert missing.category_id == "184656"
    assert missing.category_name == "Water Pumps"
    assert already.category_id == "1000"  # untouched


def test_all_flag_reresolves_everything(
    db_session: Session,
    test_user: User,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _search(db_session, test_user, test_vehicle, "water pump")
    stale = _search(
        db_session,
        test_user,
        test_vehicle,
        "thermostat",
        category_id="1000",
        category_name="Stale",
    )
    monkeypatch.setattr(
        taxonomy_sync,
        "resolve_fitment_category",
        lambda db, q: ResolvedCategory("2000", "Fresh"),
    )
    results = taxonomy_sync.run_taxonomy_sync(db_session, resolve_all=True)

    assert len(results) == 2
    db_session.refresh(stale)
    assert stale.category_id == "2000"
    assert stale.category_name == "Fresh"


def test_unresolvable_search_left_null_and_reported(
    db_session: Session,
    test_user: User,
    test_vehicle: Vehicle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = _search(db_session, test_user, test_vehicle, "obd scanner")
    monkeypatch.setattr(taxonomy_sync, "resolve_fitment_category", lambda db, q: None)
    results = taxonomy_sync.run_taxonomy_sync(db_session, resolve_all=False)

    assert results == [(search, None)]
    db_session.refresh(search)
    assert search.category_id is None
