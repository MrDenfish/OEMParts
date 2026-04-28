"""Title-based filter for OEM-only searches.

When a search has `oem_only=True` and an `oem_number` set, listings are kept
only if their title contains the word "OEM", the word "Genuine", or the
normalized OEM part number itself. The OEM number match is included because
sellers often list a clean part number without typing the word "OEM".
"""

import re

# Word-boundary matches so we don't accept "OEMlike" or "Genuinely" oddities.
# Case-insensitive in titles is handled by lowercasing both sides.
_OEM_WORD_RE = re.compile(r"\boem\b", re.IGNORECASE)
_GENUINE_WORD_RE = re.compile(r"\bgenuine\b", re.IGNORECASE)


def _normalize_part_number(value: str) -> str:
    """Strip non-alphanumeric characters and lowercase.

    Sellers format OEM numbers inconsistently (LR036970, LR-036970, LR 036970).
    Normalizing both the title and the stored number lets all variants match.
    """
    return re.sub(r"[^a-z0-9]", "", value.lower())


def title_matches_oem(title: str, oem_number: str | None) -> bool:
    """Return True if the listing title qualifies as OEM/Genuine.

    A title qualifies if any of these is present:
      - the word "OEM" (case-insensitive)
      - the word "Genuine" (case-insensitive)
      - the normalized OEM number as a substring of the normalized title
        (only when oem_number is provided and at least 4 chars after
        normalization — shorter strings have too high a collision risk)

    Args:
        title: Listing title from eBay.
        oem_number: The OEM part number on the search, or None.

    Returns:
        True if the listing should be kept; False if it should be filtered out.
    """
    if _OEM_WORD_RE.search(title) or _GENUINE_WORD_RE.search(title):
        return True

    if oem_number:
        normalized_number = _normalize_part_number(oem_number)
        if len(normalized_number) >= 4:
            normalized_title = _normalize_part_number(title)
            if normalized_number in normalized_title:
                return True

    return False
