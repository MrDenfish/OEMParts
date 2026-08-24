# AI Relevance Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily Claude classification pass marks each (search, listing) pair as part/accessory/unrelated so accessories stop polluting digest alerts, plus AI query crafting at search creation.

**Architecture:** New `app/core/ai_relevance.py` wraps the Anthropic SDK behind two functions (`classify_listings`, `craft_query`), both fail-open (any error → None → today's behavior). Verdicts live on the `search_listings` junction (one migration). The digest's `run_digest` gains a classification step before scanning, and `scan_and_record` demotes classified-away listings from "notable" to counted. The create-search route optionally refines query text before category resolution. Everything is inert behind `AI_FILTER_ENABLED=false`.

**Tech Stack:** Python 3.11, `anthropic` SDK (new dependency), structured outputs (`output_config.format` json_schema), SQLAlchemy/Alembic, pytest with a fully mocked client.

**Spec:** `docs/superpowers/specs/2026-08-24-ai-relevance-filter-design.md`

## Global Constraints

- Verdict values exactly: `part` / `accessory` / `unrelated`; NULL = unclassified = behave as before (fail-open contract everywhere).
- At most ONE classification API call per search per run, capped at **100** unclassified links per search per run (cap logged when hit).
- `AI_FILTER_ENABLED` default **false**; empty `ANTHROPIC_API_KEY` also disables. `AI_MODEL` default **claude-opus-5** (documented swap: `claude-haiku-4-5`).
- Never log the API key. Any API error, refusal (`stop_reason == "refusal"`), or malformed response → log warning, return None, digest continues.
- `accessory`/`unrelated` are excluded from notable alerts but still counted in "other new". Existing alert dedup/retry semantics untouched.
- Tests never touch the network: monkeypatch `ai_relevance._client`. Suite hermetic wrt `.env` (monkeypatch settings).
- Type hints; mypy clean on new/modified modules (pre-existing `queries.py` debt out of scope).
- Before each commit: `.venv/bin/ruff check . && .venv/bin/ruff format .` then `git checkout -- alembic/` if ruff touched it (existing migrations stay byte-identical); `git status` clean of alembic changes.
- Never modify existing migrations; new migration tested upgrade → downgrade → upgrade. Local Postgres via `docker compose -f docker-compose.local.yml up -d db`.
- Commit messages imperative present tense.

---

### Task 1: Schema — relevance columns on search_listings

**Files:**
- Modify: `app/db/models.py` (SearchListing class, currently search_id/listing_id/matched_at around lines 213-235)
- Create: `alembic/versions/<autogen>_add_relevance_to_search_listings.py`
- Test: `tests/db/test_search_listing_relevance.py`

**Interfaces:**
- Produces: `SearchListing.relevance: str | None` (String(10)), `SearchListing.relevance_checked_at: datetime | None` (TIMESTAMPTZ) — used by Tasks 3 and 5.

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_search_listing_relevance.py`:

```python
"""Schema tests for search_listings relevance columns."""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import Listing, Search, SearchListing, utcnow


def test_relevance_defaults_to_none(db_session: Session, test_search: Search) -> None:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Part",
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
    )
    db_session.add(listing)
    db_session.flush()
    link = SearchListing(search_id=test_search.id, listing_id=listing.id)
    db_session.add(link)
    db_session.commit()
    assert link.relevance is None
    assert link.relevance_checked_at is None


def test_relevance_roundtrip(db_session: Session, test_search: Search) -> None:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Bracket",
        price=Decimal("25.00"),
        item_url="https://www.ebay.com/itm/2",
    )
    db_session.add(listing)
    db_session.flush()
    link = SearchListing(
        search_id=test_search.id,
        listing_id=listing.id,
        relevance="accessory",
        relevance_checked_at=utcnow(),
    )
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    assert link.relevance == "accessory"
    assert link.relevance_checked_at is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/db/test_search_listing_relevance.py -v`
Expected: FAIL — `'relevance' is an invalid keyword argument for SearchListing`.

- [ ] **Step 3: Add model columns**

In `app/db/models.py`, in `class SearchListing`, after the `matched_at` column:

```python
    relevance: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="AI verdict: part | accessory | unrelated; NULL = unclassified",
    )
    relevance_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/db/test_search_listing_relevance.py -v`
Expected: 2 PASS (conftest create_all uses the model).

- [ ] **Step 5: Generate + verify the migration**

```bash
docker compose -f docker-compose.local.yml up -d db
PYTHONPATH=$PWD .venv/bin/alembic revision --autogenerate -m "add relevance to search_listings"
```

Inspect: exactly two `op.add_column("search_listings", ...)` in `upgrade()` and two `op.drop_column` in `downgrade()`; delete any unrelated autogenerated noise. Then:

```bash
PYTHONPATH=$PWD .venv/bin/alembic upgrade head
PYTHONPATH=$PWD .venv/bin/alembic downgrade -1
PYTHONPATH=$PWD .venv/bin/alembic upgrade head
```

- [ ] **Step 6: Full suite, lint, commit**

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format . && git checkout -- alembic/ 2>/dev/null; git status --short
git add app/db/models.py alembic/versions/ tests/db/test_search_listing_relevance.py
git commit -m "Add relevance columns to search_listings"
```

(Note: `git checkout -- alembic/` restores formatter churn on OLD files; your NEW migration file is untracked until `git add`, so it is unaffected. Verify with `git status` that the new file is still present and old migrations are unmodified.)

---

### Task 2: AI client module + config + dependency

**Files:**
- Create: `app/core/ai_relevance.py`
- Modify: `app/config.py` (new settings), `requirements.txt` (anthropic)
- Test: `tests/core/test_ai_relevance.py`

**Interfaces:**
- Consumes: `settings.anthropic_api_key`, `settings.ai_model`; `Search`, `Listing`, `Vehicle` models.
- Produces (used by Tasks 3 and 4):
  - `classify_listings(search: Search, listings: list[Listing]) -> dict[uuid.UUID, str] | None` — maps listing.id → verdict; `{}` for empty input; None on any failure.
  - `craft_query(query_text: str, oem_number: str | None, vehicle: Vehicle, category_name: str | None) -> str | None`
  - `_client() -> anthropic.Anthropic` — the monkeypatch seam; nothing else constructs a client.
  - `VERDICTS = ("part", "accessory", "unrelated")`

- [ ] **Step 1: Add config + dependency**

`app/config.py`, after the digest settings block:

```python
    # AI relevance filter (spec docs/superpowers/specs/2026-08-24-ai-*.md)
    ai_filter_enabled: bool = False
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5"
```

`requirements.txt`, after the Clerk block:

```
# AI relevance filter (part/accessory classification + query crafting)
anthropic>=0.116,<1.0
```

Then: `.venv/bin/pip install -r requirements.txt` (installs anthropic).

- [ ] **Step 2: Write the failing tests**

Create `tests/core/test_ai_relevance.py`:

```python
"""Tests for the AI relevance client. The Anthropic client is fully mocked
via the ai_relevance._client seam — no network, no API key needed."""

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.core import ai_relevance
from app.db.models import Listing, Search, Vehicle


def make_search(**kw) -> Search:
    defaults = dict(
        query_text="AMK air suspension compressor",
        oem_number="LR072537",
        category_name="Self-Leveling Suspension Parts",
    )
    defaults.update(kw)
    return Search(**defaults)  # type: ignore[arg-type]


def make_listing(title: str, price: str) -> Listing:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title=title,
        price=Decimal(price),
        item_url="https://www.ebay.com/itm/1",
    )
    listing.id = uuid.uuid4()
    return listing


def fake_response(payload: dict, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


@pytest.fixture(autouse=True)
def _ai_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ai_filter_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "ai_model", "claude-opus-5")


def patch_client(monkeypatch: pytest.MonkeyPatch, response) -> MagicMock:
    client = MagicMock()
    if isinstance(response, Exception):
        client.messages.create.side_effect = response
    else:
        client.messages.create.return_value = response
    monkeypatch.setattr(ai_relevance, "_client", lambda: client)
    return client


class TestClassifyListings:
    def test_maps_verdicts_by_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        search = make_search()
        listings = [
            make_listing("AMK compressor unit", "230.65"),
            make_listing("Bracket mount kit", "24.99"),
        ]
        client = patch_client(
            monkeypatch,
            fake_response(
                {"verdicts": [
                    {"index": 0, "verdict": "part"},
                    {"index": 1, "verdict": "accessory"},
                ]}
            ),
        )
        result = ai_relevance.classify_listings(search, listings)
        assert result == {
            listings[0].id: "part",
            listings[1].id: "accessory",
        }
        assert client.messages.create.call_count == 1
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-5"
        prompt = kwargs["messages"][0]["content"]
        assert "AMK compressor unit" in prompt and "LR072537" in prompt

    def test_empty_input_returns_empty_without_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = patch_client(monkeypatch, fake_response({"verdicts": []}))
        assert ai_relevance.classify_listings(make_search(), []) == {}
        assert client.messages.create.call_count == 0

    def test_api_error_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        patch_client(
            monkeypatch,
            anthropic.APIConnectionError(request=MagicMock()),
        )
        result = ai_relevance.classify_listings(
            make_search(), [make_listing("x", "1.00")]
        )
        assert result is None

    def test_refusal_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_client(monkeypatch, fake_response({}, stop_reason="refusal"))
        assert (
            ai_relevance.classify_listings(make_search(), [make_listing("x", "1.00")])
            is None
        )

    def test_invalid_verdict_and_index_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listings = [make_listing("a", "1.00"), make_listing("b", "2.00")]
        patch_client(
            monkeypatch,
            fake_response(
                {"verdicts": [
                    {"index": 0, "verdict": "sideways"},   # bad verdict → skipped
                    {"index": 9, "verdict": "part"},        # bad index → skipped
                    {"index": 1, "verdict": "unrelated"},
                ]}
            ),
        )
        result = ai_relevance.classify_listings(make_search(), listings)
        assert result == {listings[1].id: "unrelated"}

    def test_malformed_json_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="not json {")],
        )
        patch_client(monkeypatch, response)
        assert (
            ai_relevance.classify_listings(make_search(), [make_listing("x", "1.00")])
            is None
        )


class TestCraftQuery:
    def _vehicle(self) -> Vehicle:
        return Vehicle(year=2012, make="Land Rover", model="LR4")  # type: ignore[call-arg]

    def test_returns_refined_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_client(
            monkeypatch, fake_response({"query": "AMK air suspension compressor"})
        )
        result = ai_relevance.craft_query(
            "Waterpump to Thermostat", "LR072537", self._vehicle(), None
        )
        assert result == "AMK air suspension compressor"

    def test_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import anthropic

        patch_client(monkeypatch, anthropic.APIConnectionError(request=MagicMock()))
        assert (
            ai_relevance.craft_query("x", None, self._vehicle(), None) is None
        )

    def test_blank_or_oversized_result_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        patch_client(monkeypatch, fake_response({"query": "   "}))
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None
        patch_client(monkeypatch, fake_response({"query": "y" * 500}))
        assert ai_relevance.craft_query("x", None, self._vehicle(), None) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/core/test_ai_relevance.py -v`
Expected: ImportError — `app.core.ai_relevance` does not exist.

- [ ] **Step 4: Implement `app/core/ai_relevance.py`**

```python
"""AI relevance classification and query crafting (fail-open).

Two touchpoints (spec §4.2): classify_listings — one call per search
batching listing titles into part/accessory/unrelated verdicts via
structured outputs — and craft_query, a one-shot query refinement at
search creation. Every failure path returns None; callers treat None
as "behave exactly as before". The API key is never logged.
"""

import json
import logging
import uuid

import anthropic

from app.config import settings
from app.db.models import Listing, Search, Vehicle

logger = logging.getLogger(__name__)

VERDICTS = ("part", "accessory", "unrelated")
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

    lines = "\n".join(
        f"{i}. {listing.title} (${listing.price})"
        for i, listing in enumerate(listings)
    )
    intent = (
        f"Search query: {search.query_text}\n"
        f"OEM part number: {search.oem_number or 'none'}\n"
        f"eBay category: {search.category_name or 'unknown'}\n"
    )
    prompt = (
        "You are classifying eBay listings for a car-parts tracking tool.\n"
        "The user's search describes ONE specific part they want to buy.\n\n"
        f"{intent}\n"
        "For each numbered listing below, decide:\n"
        "- part: this listing IS the part itself (any brand, incl. "
        "aftermarket equivalents and supersession part numbers)\n"
        "- accessory: a bracket, mount, relay, pipe, seal, tool, or other "
        "item FOR the part, not the part itself\n"
        "- unrelated: neither the part nor an accessory for it\n\n"
        f"Listings:\n{lines}"
    )

    try:
        response = _client().messages.create(
            model=settings.ai_model,
            # Thinking is on by default on current models and shares this
            # budget with the JSON output — leave headroom for both.
            max_tokens=8192,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        logger.warning("AI classification failed for search %s: %s", search.id, exc)
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
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": CRAFT_SCHEMA},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        logger.warning("AI query crafting failed: %s", exc)
        return None

    text = _first_text(response)
    if text is None:
        return None
    try:
        crafted = json.loads(text).get("query", "")
    except json.JSONDecodeError:
        return None
    crafted = crafted.strip()
    if not crafted or len(crafted) > MAX_CRAFTED_QUERY_LEN:
        return None
    return crafted
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/core/test_ai_relevance.py -v`
Expected: 9 PASS.

- [ ] **Step 6: Type-check, lint, full suite, commit**

```bash
.venv/bin/mypy app/core/ai_relevance.py
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format . && git checkout -- alembic/ 2>/dev/null; git status --short
git add app/core/ai_relevance.py app/config.py requirements.txt tests/core/test_ai_relevance.py
git commit -m "Add AI relevance client: classification and query crafting"
```

---

### Task 3: Daily classification pass + notable integration

**Files:**
- Modify: `app/db/queries.py` (three helpers), `app/core/digest.py` (classification step + notable check + summary field), `app/worker/cli.py` (print classified count)
- Test: `tests/db/test_relevance_queries.py`, additions to `tests/core/test_digest.py`

**Interfaces:**
- Consumes: Task 1 columns; Task 2 `classify_listings`; existing digest structure (`run_digest` iterates `db.query(User).all()`, calls `scan_and_record(db, user_id, window_start)`; notable check at `app/core/digest.py:81-84`).
- Produces:
  - `queries.get_unclassified_links(db, search_id, limit) -> list[SearchListing]` (active listings only, oldest matched_at first)
  - `queries.set_link_relevance(db, search_id, listing_id, verdict) -> None` (stamps relevance_checked_at)
  - `queries.get_relevance_map(db, search_id) -> dict[uuid.UUID, str]` (listing_id → verdict, classified rows only)
  - `digest.AI_CLASSIFY_CAP = 100`; `digest.classify_pending(db, user_id) -> int`
  - `DigestSummary` gains `classified: int = 0`.

- [ ] **Step 1: Write the failing query-helper tests**

Create `tests/db/test_relevance_queries.py`:

```python
"""Tests for relevance query helpers."""

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import Listing, Search, SearchListing


def link_listing(
    db: Session, search: Search, title: str = "Part", active: bool = True
) -> Listing:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title=title,
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
        is_active=active,
    )
    db.add(listing)
    db.flush()
    db.add(SearchListing(search_id=search.id, listing_id=listing.id))
    db.flush()
    return listing


def test_unclassified_links_active_only_with_limit(
    db_session: Session, test_search: Search
) -> None:
    active = link_listing(db_session, test_search)
    link_listing(db_session, test_search, active=False)
    links = queries.get_unclassified_links(db_session, test_search.id, limit=10)
    assert [link.listing_id for link in links] == [active.id]
    assert queries.get_unclassified_links(db_session, test_search.id, limit=0) == []


def test_set_and_map_relevance(db_session: Session, test_search: Search) -> None:
    listing = link_listing(db_session, test_search)
    queries.set_link_relevance(db_session, test_search.id, listing.id, "accessory")
    assert queries.get_unclassified_links(db_session, test_search.id, limit=10) == []
    mapping = queries.get_relevance_map(db_session, test_search.id)
    assert mapping == {listing.id: "accessory"}
    link = db_session.get(SearchListing, (test_search.id, listing.id))
    assert link is not None and link.relevance_checked_at is not None
```

- [ ] **Step 2: Run to verify failure, then implement the helpers**

Run: `.venv/bin/pytest tests/db/test_relevance_queries.py -v` → AttributeError.

Append to `app/db/queries.py` (after the alert section):

```python
# ---------------------------------------------------------------------------
# Relevance queries (AI filter — spec docs/superpowers/specs/2026-08-24-ai-*)
# ---------------------------------------------------------------------------


def get_unclassified_links(
    db: Session, search_id: uuid.UUID, limit: int
) -> list[SearchListing]:
    """Unclassified links to active listings, oldest first, capped."""
    return (
        db.query(SearchListing)
        .join(Listing, Listing.id == SearchListing.listing_id)
        .filter(
            SearchListing.search_id == search_id,
            SearchListing.relevance.is_(None),
            Listing.is_active.is_(True),
        )
        .order_by(SearchListing.matched_at)
        .limit(limit)
        .all()
    )


def set_link_relevance(
    db: Session, search_id: uuid.UUID, listing_id: uuid.UUID, verdict: str
) -> None:
    """Persist an AI verdict on a (search, listing) link."""
    db.execute(
        update(SearchListing)
        .where(
            SearchListing.search_id == search_id,
            SearchListing.listing_id == listing_id,
        )
        .values(relevance=verdict, relevance_checked_at=utcnow())
    )
    db.flush()


def get_relevance_map(db: Session, search_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """listing_id -> verdict for a search's classified links."""
    rows = (
        db.query(SearchListing.listing_id, SearchListing.relevance)
        .filter(
            SearchListing.search_id == search_id,
            SearchListing.relevance.is_not(None),
        )
        .all()
    )
    return {listing_id: relevance for listing_id, relevance in rows}
```

Run: `.venv/bin/pytest tests/db/test_relevance_queries.py -v` → 2 PASS.

- [ ] **Step 3: Write the failing digest-integration tests**

Append to `tests/core/test_digest.py` (reuse its existing helpers `make_listing`, `link`, `seed_baseline`, fixtures):

```python
class TestAIClassificationPass:
    def _enable_ai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ai_filter_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")

    def test_classified_accessory_not_notable(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        """An accessory verdict demotes a would-be-notable new listing."""
        seed_baseline(db_session, test_search)
        cheap_new = make_listing(db_session, "50.00", days_old=0.1)  # below p25
        link(db_session, test_search, cheap_new)
        queries.set_link_relevance(
            db_session, test_search.id, cheap_new.id, "accessory"
        )
        created, other = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 0
        assert other[test_search.id] == 1  # counted, not alerted

    def test_part_and_null_still_notable(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        seed_baseline(db_session, test_search)
        classified = make_listing(db_session, "50.00", days_old=0.1)
        link(db_session, test_search, classified)
        queries.set_link_relevance(db_session, test_search.id, classified.id, "part")
        unclassified = make_listing(db_session, "51.00", days_old=0.1)
        link(db_session, test_search, unclassified)
        created, _ = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 2

    def test_classify_pending_persists_verdicts(
        self,
        db_session: Session,
        test_user: User,
        test_search: Search,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._enable_ai(monkeypatch)
        bracket = make_listing(db_session, "24.99", days_old=0.1)
        link(db_session, test_search, bracket)
        monkeypatch.setattr(
            digest,
            "classify_listings",
            lambda search, listings: {bracket.id: "accessory"},
        )
        classified = digest.classify_pending(db_session, test_user.id)
        assert classified == 1
        assert queries.get_relevance_map(db_session, test_search.id) == {
            bracket.id: "accessory"
        }

    def test_classify_pending_fail_open(
        self,
        db_session: Session,
        test_user: User,
        test_search: Search,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._enable_ai(monkeypatch)
        bracket = make_listing(db_session, "24.99", days_old=0.1)
        link(db_session, test_search, bracket)
        monkeypatch.setattr(digest, "classify_listings", lambda s, ls: None)
        assert digest.classify_pending(db_session, test_user.id) == 0
        assert queries.get_relevance_map(db_session, test_search.id) == {}

    def test_classify_pending_disabled_no_calls(
        self,
        db_session: Session,
        test_user: User,
        test_search: Search,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ai_filter_enabled stays False (module default via _digest_settings)
        called = MagicMock()
        monkeypatch.setattr(digest, "classify_listings", called)
        assert digest.classify_pending(db_session, test_user.id) == 0
        assert called.call_count == 0
```

Also add `monkeypatch.setattr(settings, "ai_filter_enabled", False)` and `monkeypatch.setattr(settings, "anthropic_api_key", "")` lines to the existing autouse `_digest_settings` fixture so every legacy test runs with AI off.

- [ ] **Step 4: Run to verify failure, then implement**

Run: `.venv/bin/pytest tests/core/test_digest.py -v` → AttributeError on `digest.classify_pending`.

In `app/core/digest.py`:

a. Add imports: `from app.core.ai_relevance import classify_listings` and add `AI_CLASSIFY_CAP = 100` beside the other constants.

b. Add `classified: int = 0` to `DigestSummary`.

c. Add after `scan_and_record`:

```python
def classify_pending(db: Session, user_id: uuid.UUID) -> int:
    """AI-classify unclassified (search, listing) links, one call per search.

    Fail-open: disabled flag, missing key, or a failed call leaves links
    NULL — tomorrow retries. Returns the number of verdicts persisted.
    """
    if not settings.ai_filter_enabled or not settings.anthropic_api_key:
        return 0

    classified = 0
    for search in queries.get_searches_for_user(db, user_id):
        if not search.is_active:
            continue
        links = queries.get_unclassified_links(db, search.id, AI_CLASSIFY_CAP)
        if not links:
            continue
        if len(links) == AI_CLASSIFY_CAP:
            logger.info(
                "Classification cap (%d) hit for search %s; remainder tomorrow",
                AI_CLASSIFY_CAP,
                search.id,
            )
        listings = [
            listing
            for link in links
            if (listing := db.get(Listing, link.listing_id)) is not None
        ]
        verdicts = classify_listings(search, listings)
        if verdicts is None:
            continue
        for listing_id, verdict in verdicts.items():
            queries.set_link_relevance(db, search.id, listing_id, verdict)
            classified += 1
        db.commit()
    return classified
```

d. In `scan_and_record`, before the per-listing loop add `relevance_map = queries.get_relevance_map(db, search.id)` (directly after the `stats = ...` line), and change the notable branch to demote classified-away listings:

```python
            # Signal 2: new listing in the window — notable or just counted.
            if listing.first_seen_at >= window_start:
                price = listing.price
                assert isinstance(price, Decimal), "Listing.price must be Decimal"
                verdict = relevance_map.get(listing.id)
                notable = verdict not in ("accessory", "unrelated") and (
                    is_low_in_search(price, stats)
                    or (
                        search.oem_number is not None
                        and title_contains_part_number(
                            listing.title, search.oem_number
                        )
                    )
                )
```

(The `else: other_new[...]` branch is unchanged — demoted listings fall into it.)

e. In `run_digest`, inside the per-user loop, before `scan_and_record`:

```python
        total_classified += classify_pending(db, user.id)
```

with `total_classified = 0` initialized beside `total_created`, and `classified=total_classified` added to the returned `DigestSummary`.

f. In `app/worker/cli.py` `cmd_digest`, add after the existing prints:

```python
        print(f"classified:     {summary.classified}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/core/test_digest.py tests/db/ tests/worker/ -v`
Expected: all PASS (legacy digest tests still green with AI off).

- [ ] **Step 6: Type-check, lint, full suite, commit**

```bash
.venv/bin/mypy app/core/digest.py
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format . && git checkout -- alembic/ 2>/dev/null; git status --short
git add app/db/queries.py app/core/digest.py app/worker/cli.py tests/db/test_relevance_queries.py tests/core/test_digest.py
git commit -m "Classify listings daily and demote accessories from notable alerts"
```

---

### Task 4: Creation-time query crafting

**Files:**
- Modify: `app/web/routes/searches.py` (`create_search` route)
- Test: additions to `tests/web/test_search_create_category.py`

**Interfaces:**
- Consumes: Task 2 `craft_query(query_text, oem_number, vehicle, category_name) -> str | None`; existing `queries.get_vehicle_by_id(db, vehicle_id, user_id)`.
- Produces: refined query stored as `query_text` and used for category resolution; user text kept verbatim on any failure or when AI disabled.

- [ ] **Step 1: Write the failing tests**

Append to `tests/web/test_search_create_category.py`:

```python
class TestAIQueryCrafting:
    def test_crafted_query_stored_and_resolved(
        self,
        client: TestClient,
        db_session: Session,
        test_vehicle: Vehicle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "ai_filter_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
        monkeypatch.setattr(
            searches_module, "craft_query", lambda q, o, v, c: "water pump hose"
        )
        seen: dict = {}

        def fake_resolve(db, query):
            seen["query"] = query
            return None

        monkeypatch.setattr(searches_module, "resolve_fitment_category", fake_resolve)
        client.post(
            "/searches/",
            data={"vehicle_id": str(test_vehicle.id), "query_text": "Waterpump to Thermostat"},
            follow_redirects=False,
        )
        search = db_session.query(Search).filter_by(query_text="water pump hose").one()
        assert search is not None
        assert seen["query"] == "water pump hose"

    def test_crafting_failure_keeps_user_text(
        self,
        client: TestClient,
        db_session: Session,
        test_vehicle: Vehicle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "ai_filter_enabled", True)
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
        monkeypatch.setattr(searches_module, "craft_query", lambda q, o, v, c: None)
        monkeypatch.setattr(
            searches_module, "resolve_fitment_category", lambda db, q: None
        )
        client.post(
            "/searches/",
            data={"vehicle_id": str(test_vehicle.id), "query_text": "my exact words"},
            follow_redirects=False,
        )
        assert (
            db_session.query(Search).filter_by(query_text="my exact words").one()
            is not None
        )

    def test_disabled_never_calls_craft(
        self,
        client: TestClient,
        db_session: Session,
        test_vehicle: Vehicle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        called = MagicMock()
        monkeypatch.setattr(searches_module, "craft_query", called)
        monkeypatch.setattr(
            searches_module, "resolve_fitment_category", lambda db, q: None
        )
        client.post(
            "/searches/",
            data={"vehicle_id": str(test_vehicle.id), "query_text": "plain"},
            follow_redirects=False,
        )
        assert called.call_count == 0
```

Add to the file's imports: `from unittest.mock import MagicMock`, `from app.config import settings`, and `Search` from `app.db.models` (keep existing imports intact).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/web/test_search_create_category.py -v`
Expected: AttributeError — `searches_module.craft_query` doesn't exist.

- [ ] **Step 3: Implement in `app/web/routes/searches.py`**

Add import: `from app.core.ai_relevance import craft_query`.

In `create_search`, after `default_oem_only = ...` and BEFORE the category-resolution block, insert:

```python
    # AI query crafting (spec §4.5): refine the query text before category
    # resolution so both benefit. Best-effort — any failure keeps the
    # user's text verbatim.
    final_query = query_text.strip()
    if settings.ai_filter_enabled and settings.anthropic_api_key:
        vehicle = queries.get_vehicle_by_id(db, vehicle_id, current_user.id)
        if vehicle is not None:
            crafted = craft_query(final_query, parsed_oem_number, vehicle, None)
            if crafted:
                final_query = crafted
```

Add `from app.config import settings` to the module imports if absent. Then replace the two later uses of the raw text: the category-resolution call becomes `resolve_fitment_category(db, final_query)` and the `queries.create_search(...)` call passes `query_text=final_query`.

- [ ] **Step 4: Run tests, full suite**

Run: `.venv/bin/pytest tests/web/ -v` then `.venv/bin/pytest`
Expected: all PASS (existing create tests unaffected — AI off by default in their fixtures).

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/mypy app/web/routes/searches.py
.venv/bin/ruff check . && .venv/bin/ruff format . && git checkout -- alembic/ 2>/dev/null; git status --short
git add app/web/routes/searches.py tests/web/test_search_create_category.py
git commit -m "Craft search queries with AI at creation time"
```

---

### Task 5: Dashboard audit tags

**Files:**
- Modify: `app/web/routes/listings.py` (extend `listing_extras`), `app/web/templates/components/listing_table.html` (tags)
- Test: additions to `tests/web/test_price_badges.py`

**Interfaces:**
- Consumes: Task 3 `queries.get_relevance_map`; existing `listing_extras` dict built in `listings_page` (keys `drop`, `low`) and the owned-search `stats` guard added there previously.
- Produces: `listing_extras[listing.id]["relevance"]` (`str | None`); template tags `acc` / `off`.

- [ ] **Step 1: Write the failing test**

Append to `tests/web/test_price_badges.py`:

```python
def test_relevance_tags_render(
    authed_client: TestClient, db_session: Session, test_search: Search
) -> None:
    from app.db import queries

    dropper = seed(db_session, test_search)  # 5 listings incl. the 25.00 one
    queries.set_link_relevance(db_session, test_search.id, dropper.id, "accessory")
    db_session.commit()
    page = authed_client.get(f"/listings/?search_id={test_search.id}")
    assert page.status_code == 200
    assert "Classified as an accessory for this search" in page.text
```

(Use the same client fixture name the file already uses for the badge tests — if those use `client`, use `client` here instead; match the file.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/web/test_price_badges.py -v`
Expected: new test FAILS (tooltip text absent).

- [ ] **Step 3: Implement**

In `app/web/routes/listings.py`, where `stats` is computed for the owned search, also compute `relevance_map = queries.get_relevance_map(db, search_id) if owned_search else {}` (reusing the same ownership guard variable the route already has; `{}` when no search filter). Extend the `listing_extras` comprehension with `"relevance": relevance_map.get(listing.id),`.

In `app/web/templates/components/listing_table.html`, after the low-badge block inside the same `listing-meta` conditional group:

```html
            {% if extras and extras.relevance == 'accessory' %}
                <span class="badge badge-inactive" title="Classified as an accessory for this search">acc</span>
            {% elif extras and extras.relevance == 'unrelated' %}
                <span class="badge badge-inactive" title="Classified as unrelated to this search">off</span>
            {% endif %}
```

- [ ] **Step 4: Run tests, full suite**

Run: `.venv/bin/pytest tests/web/ -v` then `.venv/bin/pytest`
Expected: all PASS.

- [ ] **Step 5: Type-check, lint, commit**

```bash
.venv/bin/mypy app/web/routes/listings.py
.venv/bin/ruff check . && .venv/bin/ruff format . && git checkout -- alembic/ 2>/dev/null; git status --short
git add app/web/routes/listings.py app/web/templates/components/listing_table.html tests/web/test_price_badges.py
git commit -m "Show AI relevance audit tags on listings"
```

---

### Task 6: Docs + verification

**Files:**
- Modify: `.env.example`, `docs/SYSTEM_CONTEXT.md`

- [ ] **Step 1: Document config**

`.env.example` — add after the digest block:

```bash
# ── AI relevance filter ──
AI_FILTER_ENABLED=false              # true = classify listings daily + craft queries at creation
ANTHROPIC_API_KEY=                   # console.anthropic.com (pay-as-you-go, separate from Claude subscription)
AI_MODEL=claude-opus-5               # or claude-haiku-4-5 for ~5x lower cost
```

- [ ] **Step 2: Update SYSTEM_CONTEXT.md**

- §5: in the digest row of the schedule table (or adjacent prose), note the digest run now includes the AI classification step when enabled.
- Changelog (newest-first, dated with actual completion date): new `search_listings.relevance` columns (migration id), `app/core/ai_relevance.py` (one structured-output call per search per day, cap 100 links, fail-open, `AI_FILTER_ENABLED` default off, `AI_MODEL` swap documented), digest notable demotion for accessory/unrelated, creation-time query crafting, dashboard `acc`/`off` audit tags, `anthropic` dependency added, test count.

- [ ] **Step 3: Full verification + commit**

```bash
.venv/bin/pytest
.venv/bin/mypy app/core/ai_relevance.py app/core/digest.py
.venv/bin/ruff check . && .venv/bin/ruff format . && git checkout -- alembic/ 2>/dev/null; git status --short
git add .env.example docs/SYSTEM_CONTEXT.md
git commit -m "Update SYSTEM_CONTEXT: AI relevance filter"
```

---

### Task 7: Live validation (manual, with owner)

**Files:** none (operational; findings may spawn fix commits).

- [ ] **Step 1:** Owner creates an Anthropic API key at console.anthropic.com (pay-as-you-go billing, separate from the Claude subscription) and sets `.env`: `AI_FILTER_ENABLED=true`, `ANTHROPIC_API_KEY=sk-ant-...`.
- [ ] **Step 2:** Run `.venv/bin/python oemparts digest` manually. Expect: `classified: N` covering the backlog (capped at 100/search — the AMK compressor search's ~30 listings classify in one call). Inspect verdicts: `SELECT sl.relevance, left(l.title,60) FROM search_listings sl JOIN listings l ON l.id=sl.listing_id WHERE sl.relevance IS NOT NULL ORDER BY sl.relevance;` — brackets/mounts should be `accessory`, compressors `part`.
- [ ] **Step 3:** Dashboard audit: filter listings by the compressor search; `acc` tags on bracket kits.
- [ ] **Step 4:** Create a throwaway search with a sloppy query (e.g. "Waterpump to Thermostat" style) and confirm the stored query is the crafted version and the category resolves sensibly; delete it after.
- [ ] **Step 5:** Overnight soak: tomorrow's digest email should exclude accessories from notable lines. Cost check at console.anthropic.com after a few days (expect cents).
- [ ] **Step 6:** If real API responses differ from assumptions (schema, refusals), fix against reality, re-run Task 2/3 tests, commit. Push and open the PR after owner sign-off.
