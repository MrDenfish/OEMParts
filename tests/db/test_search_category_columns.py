"""Schema tests for the searches.category_id / category_name columns."""

from sqlalchemy.orm import Session

from app.db.models import Search, User, Vehicle


def test_search_category_defaults_to_none(test_search: Search) -> None:
    assert test_search.category_id is None
    assert test_search.category_name is None


def test_search_category_roundtrip(
    db_session: Session, test_user: User, test_vehicle: Vehicle
) -> None:
    search = Search(
        user_id=test_user.id,
        vehicle_id=test_vehicle.id,
        query_text="water pump",
        category_id="184656",
        category_name="Water Pumps",
    )
    db_session.add(search)
    db_session.commit()
    db_session.refresh(search)
    assert search.category_id == "184656"
    assert search.category_name == "Water Pumps"
