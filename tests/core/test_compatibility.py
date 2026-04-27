"""Tests for compatibility filter builder."""

from app.core.compatibility import build_compatibility_filter


def test_basic_filter() -> None:
    result = build_compatibility_filter(2012, "Land Rover", "LR4")
    assert result == "Year:2012,Make:Land Rover,Model:LR4"


def test_filter_with_different_vehicle() -> None:
    result = build_compatibility_filter(1967, "Ford", "Mustang")
    assert result == "Year:1967,Make:Ford,Model:Mustang"


def test_filter_preserves_spaces_in_make() -> None:
    result = build_compatibility_filter(2020, "Aston Martin", "DB11")
    assert "Make:Aston Martin" in result
