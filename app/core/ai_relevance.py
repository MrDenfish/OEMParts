"""AI relevance classification and query crafting (fail-open).

Two touchpoints (spec §4.2): classify_listings — one call per search
batching listing titles into part/accessory/unrelated verdicts via
structured outputs — and craft_query, a one-shot query refinement at
search creation. Every failure path returns None; callers treat None
as "behave exactly as before". The API key is never logged.

Listing titles are seller-controlled text interpolated into prompts;
structured outputs (JSON schema, enum verdicts) bound the blast radius
of a hostile title to a wrong verdict, not arbitrary model behavior.
"""

import json
import logging
import uuid

import anthropic
from anthropic.types.message_create_params import OutputConfigParam

from app.config import settings
from app.db.models import Listing, Search, Vehicle

logger = logging.getLogger(__name__)

VERDICTS = ("part", "accessory", "unrelated", "offbrand")
MAX_CRAFTED_QUERY_LEN = 200

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                },
                "required": ["index", "verdict"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

CRAFT_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def _client() -> anthropic.Anthropic:
    """Client factory — the single construction point and the test seam."""
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _output_config(schema: dict) -> OutputConfigParam:
    """effort is supported on Opus 4.5+/Sonnet 4.6+/Opus 5 tiers but errors
    on Haiku 4.5 — omit it there so the documented cheap swap keeps working."""
    config: OutputConfigParam = {"format": {"type": "json_schema", "schema": schema}}
    if "haiku" not in settings.ai_model:
        config["effort"] = "low"
    return config


def _first_text(response: object) -> str | None:
    """Extract the first text block, or None (refusal / empty content)."""
    if getattr(response, "stop_reason", None) == "refusal":
        return None
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def classify_listings(
    search: Search, listings: list[Listing]
) -> dict[uuid.UUID, str] | None:
    """Classify listings for one search. None on failure; {} for no input."""
    if not listings:
        return {}

    def _listing_line(i: int, listing: Listing) -> str:
        line = f"{i}. {listing.title} (${listing.price})"
        if listing.brand:
            line += f" — Brand: {listing.brand}"
        if listing.mpn:
            line += f" — MPN: {listing.mpn}"
        if listing.oe_part_number:
            line += f" — OE#: {listing.oe_part_number}"
        return line

    lines = "\n".join(_listing_line(i, listing) for i, listing in enumerate(listings))
    intent = (
        f"Search query: {search.query_text}\n"
        f"OEM part number: {search.oem_number or 'none'}\n"
        f"eBay category: {search.category_name or 'unknown'}\n"
    )
    vehicle = search.vehicle
    if vehicle is not None:
        intent += f"Vehicle: {vehicle.year} {vehicle.make} {vehicle.model}\n"
    prompt = (
        "You are classifying eBay listings for a car-parts tracking tool.\n"
        "The user's search describes ONE specific part they want to buy.\n\n"
        f"{intent}\n"
        "For each numbered listing below, decide:\n"
        "- part: this listing IS the part itself (incl. aftermarket "
        "equivalents when the query names no brand, genuine/OE items, "
        "and supersession part numbers)\n"
        "- accessory: a bracket, mount, relay, pipe, seal, tool, or other "
        "item FOR the part, not the part itself\n"
        "- unrelated: neither the part nor an accessory for it\n"
        "- offbrand: ONLY when the search query names a brand or "
        "manufacturer AND this listing is a functional substitute from a "
        "positively identified different brand\n\n"
        "Brand/MPN/OE# fields come from eBay Item Specifics — trust them "
        "over words in the title (sellers put brand names in titles to "
        "catch searches). Genuine/OE items for the vehicle are 'part', "
        "never 'offbrand'. A missing or 'Unbranded' brand is NEVER "
        "grounds for 'offbrand'. An OE# matching the search's OEM part "
        "number is strong evidence of 'part'. If the query names no "
        "brand, never use 'offbrand'.\n\n"
        f"Listings:\n{lines}"
    )

    # Thinking is on by default on current models and shares this budget
    # with the JSON output — scale with batch size, capped at 16384, so
    # large batches get headroom without over-budgeting small ones.
    max_tokens = min(16384, 2048 + 60 * len(listings))
    try:
        response = _client().messages.create(
            model=settings.ai_model,
            max_tokens=max_tokens,
            output_config=_output_config(CLASSIFY_SCHEMA),
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        logger.warning("AI classification failed for search %s: %s", search.id, exc)
        return None

    if getattr(response, "stop_reason", None) == "max_tokens":
        logger.warning(
            "AI classification truncated (max_tokens) for search %s — %d listings",
            search.id,
            len(listings),
        )
        return None

    text = _first_text(response)
    if text is None:
        logger.warning("AI classification refused/empty for search %s", search.id)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("AI classification returned non-JSON for search %s", search.id)
        return None
    if not isinstance(data, dict):
        logger.warning(
            "AI classification returned non-object JSON for search %s", search.id
        )
        return None

    result: dict[uuid.UUID, str] = {}
    for entry in data.get("verdicts", []):
        index = entry.get("index")
        verdict = entry.get("verdict")
        if (
            isinstance(index, int)
            and 0 <= index < len(listings)
            and verdict in VERDICTS
        ):
            result[listings[index].id] = verdict
    return result


def craft_query(
    query_text: str,
    oem_number: str | None,
    vehicle: Vehicle,
    category_name: str | None,
) -> str | None:
    """Refine a search query for eBay matching. None on any failure."""
    prompt = (
        "Rewrite this eBay search query for a car-parts tracker. Rules:\n"
        "- keep or add the part's common noun (e.g. 'hose', 'compressor')\n"
        "- keep brand/manufacturer words if implied by the part number\n"
        "- drop connector words like 'to', 'and', 'for' (eBay ANDs all "
        "words, so extra words over-narrow results)\n"
        "- do NOT include the vehicle year/make/model (fitment filtering "
        "handles that)\n"
        "- do NOT invent part numbers; do NOT include the part number in "
        "the query\n"
        "- return 2-5 words\n\n"
        f"Vehicle: {vehicle.year} {vehicle.make} {vehicle.model}\n"
        f"OEM part number: {oem_number or 'none'}\n"
        f"eBay category: {category_name or 'unknown'}\n"
        f"User's query: {query_text}"
    )
    try:
        response = _client().messages.create(
            model=settings.ai_model,
            max_tokens=1024,
            output_config=_output_config(CRAFT_SCHEMA),
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        logger.warning("AI query crafting failed: %s", exc)
        return None

    text = _first_text(response)
    if text is None:
        logger.warning("AI query crafting refused/empty")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("AI query crafting returned non-JSON")
        return None
    if not isinstance(data, dict):
        logger.warning("AI query crafting returned non-object JSON")
        return None
    crafted = data.get("query", "")
    if not isinstance(crafted, str):
        logger.warning("AI query crafting returned non-string query")
        return None
    crafted = crafted.strip()
    if not crafted or len(crafted) > MAX_CRAFTED_QUERY_LEN:
        return None
    return crafted
