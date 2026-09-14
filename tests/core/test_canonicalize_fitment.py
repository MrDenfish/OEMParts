"""Tests for canonicalizing make/model to eBay's fitment catalog spelling.

eBay's compatibility_filter is case-sensitive: "LAND ROVER" silently matched
48 listings where "Land Rover" matched 125 (verified live 2026-09-14). These
functions map any spelling variant onto eBay's one canonical value; when no
match is found (or the API is down) the input is returned unchanged.

The taxonomy lookup is monkeypatched — canonicalization logic only here.
"""

import pytest
from sqlalchemy.orm import Session

from app.core import compatibility

MAKES = ["BMW", "Land Rover", "Mercedes-Benz", "Toyota"]
LAND_ROVER_MODELS = ["Defender", "LR4", "Range Rover", "Range Rover Sport"]


@pytest.fixture(autouse=True)
def _fake_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(db, category_id, property_name, filter_make=None):
        if property_name == "Make":
            return MAKES
        if property_name == "Model" and filter_make == "Land Rover":
            return LAND_ROVER_MODELS
        return []

    monkeypatch.setattr(compatibility, "get_compatibility_property_values", fake)


@pytest.mark.parametrize(
    "raw",
    ["LAND ROVER", "land rover", "Land rover", "Land ROVER", "LandROVER", "Land Rover"],
)
def test_make_variants_map_to_canonical(db_session: Session, raw: str) -> None:
    assert compatibility.canonicalize_make(db_session, raw) == "Land Rover"


def test_acronym_makes_keep_their_casing(db_session: Session) -> None:
    assert compatibility.canonicalize_make(db_session, "bmw") == "BMW"


def test_hyphenated_make_matches_without_hyphen(db_session: Session) -> None:
    assert (
        compatibility.canonicalize_make(db_session, "mercedes benz") == "Mercedes-Benz"
    )


def test_unknown_make_returned_unchanged(db_session: Session) -> None:
    assert (
        compatibility.canonicalize_make(db_session, "Frankenmotors") == "Frankenmotors"
    )


def test_empty_make_returned_unchanged(db_session: Session) -> None:
    assert compatibility.canonicalize_make(db_session, "") == ""


@pytest.mark.parametrize("raw", ["lr4", "LR4", "Lr4", "LR-4"])
def test_model_variants_map_to_canonical(db_session: Session, raw: str) -> None:
    assert compatibility.canonicalize_model(db_session, "Land Rover", raw) == "LR4"


def test_unknown_model_returned_unchanged(db_session: Session) -> None:
    assert (
        compatibility.canonicalize_model(db_session, "Land Rover", "Hovercraft")
        == "Hovercraft"
    )


def test_api_failure_returns_input_unchanged(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        compatibility,
        "get_compatibility_property_values",
        lambda db, category_id, property_name, filter_make=None: None,
    )
    assert compatibility.canonicalize_make(db_session, "LAND ROVER") == "LAND ROVER"
    assert compatibility.canonicalize_model(db_session, "LAND ROVER", "lr4") == "lr4"
