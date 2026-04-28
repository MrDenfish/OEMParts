"""Tests for the OEM-only title filter."""

from app.core.oem_filter import title_matches_oem


def test_oem_word_in_title_matches() -> None:
    assert title_matches_oem("OEM Land Rover LR4 Coolant Pipe", "LR036970") is True


def test_oem_word_case_insensitive() -> None:
    assert title_matches_oem("oem land rover coolant pipe", None) is True
    assert title_matches_oem("Oem Coolant Pipe", None) is True


def test_genuine_word_in_title_matches() -> None:
    assert title_matches_oem("Genuine Land Rover Water Pump", "LR036970") is True


def test_genuine_word_case_insensitive() -> None:
    assert title_matches_oem("genuine land rover water pump", None) is True


def test_aftermarket_title_rejected() -> None:
    assert title_matches_oem("Aftermarket Land Rover Coolant Pipe", "LR036970") is False


def test_oem_number_substring_matches_when_clean() -> None:
    assert title_matches_oem("LR036970 Coolant Pipe Land Rover", "LR036970") is True


def test_oem_number_substring_matches_with_hyphens() -> None:
    # Title has the part number formatted with a hyphen; stored number is clean.
    assert title_matches_oem("Coolant Pipe LR-036970 Fits LR4", "LR036970") is True


def test_oem_number_substring_matches_with_spaces() -> None:
    assert title_matches_oem("Pipe LR 036970 Fits LR4", "LR036970") is True


def test_stored_number_with_hyphens_normalizes() -> None:
    # User entered hyphenated stored number; title is clean.
    assert title_matches_oem("Coolant Pipe LR036970 Fits LR4", "LR-036970") is True


def test_no_oem_word_no_part_number_rejected() -> None:
    assert (
        title_matches_oem("Land Rover LR4 Coolant Pipe Replacement", "LR036970")
        is False
    )


def test_oem_substring_in_other_word_does_not_match() -> None:
    # "Genuinely" should not match "Genuine" word boundary.
    # We're being strict on word boundary for OEM/Genuine.
    assert title_matches_oem("Genuinely Great Aftermarket Pipe", None) is False


def test_oem_substring_in_other_word_oem_does_not_match() -> None:
    # Avoid false positives like "POEM" or "OEMlike".
    assert title_matches_oem("Poem about pipes", None) is False


def test_short_oem_number_skipped() -> None:
    # Numbers under 4 normalized chars are too collision-prone to use as substring.
    # Title has "abc" but no OEM/Genuine word.
    assert title_matches_oem("abc Coolant Pipe", "abc") is False


def test_oem_number_none_falls_back_to_word_match() -> None:
    assert title_matches_oem("OEM Coolant Pipe", None) is True
    assert title_matches_oem("Aftermarket Coolant Pipe", None) is False
