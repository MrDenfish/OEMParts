"""Tests for compatibility filter building and fitment category resolution."""

import pytest
from sqlalchemy.orm import Session

from app.core.compatibility import (
    ResolvedCategory,
    build_compatibility_filter,
    resolve_fitment_category,
)
from app.sources import ebay_taxonomy
from app.sources.ebay_taxonomy import CategorySuggestion


class TestBuildCompatibilityFilter:
    def test_basic_filter(self) -> None:
        result = build_compatibility_filter(2012, "Land Rover", "LR4")
        assert result == "Year:2012;Make:Land Rover;Model:LR4"

    def test_filter_with_different_vehicle(self) -> None:
        result = build_compatibility_filter(1999, "Mazda", "Miata")
        assert result == "Year:1999;Make:Mazda;Model:Miata"

    def test_filter_preserves_spaces_in_make(self) -> None:
        result = build_compatibility_filter(2015, "Alfa Romeo", "4C")
        assert result == "Year:2015;Make:Alfa Romeo;Model:4C"


MOTORS_ANCESTORS = ["6000", "6028", "33559"]


class TestResolveFitmentCategory:
    def test_picks_first_motors_suggestion_with_ymm_support(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ebay_taxonomy,
            "get_category_suggestions",
            lambda db, q: [
                CategorySuggestion("184656", "Water Pumps", MOTORS_ANCESTORS),
            ],
        )
        monkeypatch.setattr(
            ebay_taxonomy,
            "get_compatibility_properties",
            lambda db, cid: ["Year", "Make", "Model", "Trim"],
        )
        result = resolve_fitment_category(db_session, "water pump")
        assert result == ResolvedCategory("184656", "Water Pumps")

    def test_skips_non_motors_suggestions(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ebay_taxonomy,
            "get_category_suggestions",
            lambda db, q: [
                CategorySuggestion("11700", "Home & Garden", ["11700"]),
                CategorySuggestion("184656", "Water Pumps", MOTORS_ANCESTORS),
            ],
        )
        monkeypatch.setattr(
            ebay_taxonomy,
            "get_compatibility_properties",
            lambda db, cid: ["Year", "Make", "Model"],
        )
        result = resolve_fitment_category(db_session, "water pump")
        assert result is not None
        assert result.category_id == "184656"

    def test_skips_motors_category_without_ymm(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ebay_taxonomy,
            "get_category_suggestions",
            lambda db, q: [
                CategorySuggestion("50000", "Decals", MOTORS_ANCESTORS),
            ],
        )
        monkeypatch.setattr(
            ebay_taxonomy, "get_compatibility_properties", lambda db, cid: []
        )
        assert resolve_fitment_category(db_session, "decal") is None

    def test_returns_none_when_no_suggestions(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ebay_taxonomy, "get_category_suggestions", lambda db, q: [])
        assert resolve_fitment_category(db_session, "obd scanner") is None

    def test_returns_none_on_api_exception(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(db, q):
            raise RuntimeError("network down")

        monkeypatch.setattr(ebay_taxonomy, "get_category_suggestions", boom)
        assert resolve_fitment_category(db_session, "water pump") is None
