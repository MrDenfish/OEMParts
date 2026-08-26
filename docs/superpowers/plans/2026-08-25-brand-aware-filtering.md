# Brand-Aware Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture eBay Item Specifics (Brand, MPN, OE/OEM Part Number) per listing via `getItem`, feed them to the AI classifier, and add an `offbrand` verdict that demotes competing-brand substitutes from digest alerts.

**Architecture:** A new `ebay_item` source module makes one quota-logged `getItem` call per listing (once ever, fail-soft). The worker enriches listings at the end of each fetch cycle (cap 200/cycle). The classifier prompt gains Brand/MPN/OE# ground-truth lines and a fourth enum verdict `offbrand`; digest demotion and dashboard tags extend the existing relevance machinery.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x, Alembic, httpx, anthropic SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-brand-aware-filtering-design.md` (binding)

## Global Constraints

- Fail-soft/fail-open everywhere: any `getItem` failure leaves `aspects_fetched_at` NULL (retried later); NULL relevance = today's behavior; classifier failure returns None.
- `getItem` calls happen ONLY in the worker (fetch cycle), never in request handlers.
- Every eBay call is logged to `api_quota_log` (provider `ebay_item`).
- Aspect lookup happens at most once per listing ever (except retry-after-failure); cap 200 per cycle, cap hit logged.
- `offbrand` only when the search query names a brand AND a different brand is positively identified; missing or "Unbranded" brand is NEVER `offbrand`.
- Verdict storage stays `String(10)` — `offbrand` (8 chars) fits; NO migration on `search_listings`.
- No secrets/tokens logged. Money is `Decimal`. Timestamps UTC-aware. User-scoped queries filter by `user_id` (listings are shared/public — unchanged).
- Tests hermetic: mock `httpx` for eBay, mock the `_client` seam for Anthropic; test DB `oemparts_test` only.
- Never modify existing Alembic migrations; new migration only.
- Commits: imperative present tense, ending with the two standard trailer lines (Co-Authored-By + Claude-Session) used on this branch.
- Run `ruff check . && ruff format . && mypy app/ && pytest` before each commit; alembic files must never be reformatted (`git checkout -- alembic/` if churned).

---

### Task 1: Migration + model columns

**Files:**
- Modify: `app/db/models.py` (Listing, after `category_id` ~line 199)
- Create: `alembic/versions/<autogen>_add_listing_aspects.py`
- Test: `tests/db/test_listing_aspects.py`

**Interfaces:**
- Produces: `Listing.brand: str | None`, `Listing.mpn: str | None`, `Listing.oe_part_number: str | None`, `Listing.aspects_fetched_at: datetime | None` — consumed by Tasks 2-5.

- [ ] **Step 1: Add columns to the model** — in `app/db/models.py` inside `class Listing`, directly after the `category_id` column:

```python
    brand: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="eBay Item Specifics Brand aspect"
    )
    mpn: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Manufacturer Part Number aspect"
    )
    oe_part_number: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="OE/OEM Part Number aspect (verbatim)"
    )
    aspects_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When getItem aspects lookup succeeded; NULL = pending/retry",
    )
```

- [ ] **Step 2: Generate the migration**

Run: `PYTHONPATH=$PWD alembic revision --autogenerate -m "add listing aspects"`
Inspect: it must ONLY add these four nullable columns to `listings` (drop them in `downgrade`). Delete any stray autogen noise.

- [ ] **Step 3: Apply, roll back, re-apply**

Run: `PYTHONPATH=$PWD alembic upgrade head && PYTHONPATH=$PWD alembic downgrade -1 && PYTHONPATH=$PWD alembic upgrade head`

- [ ] **Step 4: Write the test** — `tests/db/test_listing_aspects.py` (mirror the style of `tests/db/test_search_listing_relevance.py`: create a listing via the existing test fixtures, verify all four columns default to NULL, set and read back values, confirm `aspects_fetched_at` round-trips tz-aware).

- [ ] **Step 5: Run suite, commit**

Run: `pytest` (all green) then commit: `Add brand/MPN/OE aspect columns to listings`

---

### Task 2: eBay item detail client

**Files:**
- Create: `app/sources/ebay_item.py`
- Test: `tests/sources/test_ebay_item.py`

**Interfaces:**
- Consumes: `get_ebay_token(db)` from `app.sources.ebay_oauth`; `_log_api_call` pattern from `ebay_browse.py:150` (reimplement locally with provider `"ebay_item"` — do not import the private helper).
- Produces: `ItemAspects(brand, mpn, oe_part_number)` dataclass and `fetch_item_aspects(db: Session, ebay_item_id: str) -> ItemAspects | None` — consumed by Task 3. Contract: `ItemAspects` (possibly all-None fields) = lookup succeeded, stamp `aspects_fetched_at`; `None` = transient failure, leave NULL for retry. HTTP 404/410 count as success-with-nothing.

- [ ] **Step 1: Write failing tests** — `tests/sources/test_ebay_item.py`, monkeypatching `httpx.get` (same style as existing `tests/sources/` eBay tests). Cases:

```python
def _response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload or {},
                          request=httpx.Request("GET", "https://api.ebay.com/x"))

# 1. 200 with top-level brand + aspects -> all three fields extracted
#    payload: {"brand": "Dorman", "localizedAspects": [
#        {"name": "Brand", "value": "Dorman"},
#        {"name": "Manufacturer Part Number", "value": "949-919"},
#        {"name": "OE/OEM Part Number", "value": "LR124471"}]}
# 2. 200 with NO top-level brand but a Brand aspect -> brand from aspect
# 3. 200 with no aspects at all -> ItemAspects(None, None, None)  (success!)
# 4. 404 -> ItemAspects(None, None, None)  (ended listing = success-with-nothing)
# 5. 500 -> None; httpx.ConnectError raised -> None
# 6. values longer than 100 chars -> truncated to 100
# 7. every call (including 404/500) inserts one ApiQuotaLog row with provider "ebay_item"
```

- [ ] **Step 2: Run tests to verify they fail** — `pytest tests/sources/test_ebay_item.py -v` → import error.

- [ ] **Step 3: Implement** — `app/sources/ebay_item.py`:

```python
"""eBay Browse getItem client: structured Item Specifics per listing.

The search endpoint's summaries carry no brand data; sellers game titles
(live example: "Amk Air Ride Compressor" with Brand: Dorman). One getItem
call per listing captures Brand / MPN / OE-OEM Part Number as ground truth.
Fail-soft: transient errors return None so the caller retries next cycle.
"""

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import ApiQuotaLog
from app.sources.ebay_oauth import get_ebay_token

logger = logging.getLogger(__name__)

ITEM_API_URL = "https://api.ebay.com/buy/browse/v1/item/"
ITEM_SANDBOX_URL = "https://api.sandbox.ebay.com/buy/browse/v1/item/"
_MAX_LEN = 100
# 404 = listing gone, 410 = permanently removed: both are final answers,
# not transient failures — treat as success-with-nothing so we never retry.
_GONE_STATUSES = (404, 410)


@dataclass
class ItemAspects:
    brand: str | None
    mpn: str | None
    oe_part_number: str | None


def _get_item_url() -> str:
    if settings.ebay_env == "sandbox":
        return ITEM_SANDBOX_URL
    return ITEM_API_URL


def _log_api_call(db: Session, status_code: int | None) -> None:
    db.add(ApiQuotaLog(provider="ebay_item", status_code=status_code))
    db.flush()


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value[:_MAX_LEN] if value else None


def fetch_item_aspects(db: Session, ebay_item_id: str) -> ItemAspects | None:
    """Fetch Brand/MPN/OE# for one listing. None = transient failure (retry).

    An ItemAspects result (even all-None) means the lookup is final — the
    caller stamps aspects_fetched_at and never asks again.
    """
    token = get_ebay_token(db)
    url = f"{_get_item_url()}v1%7C{ebay_item_id}%7C0"
    try:
        response = httpx.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        _log_api_call(db, None)
        logger.warning("getItem failed for %s: %s", ebay_item_id, exc)
        return None

    _log_api_call(db, response.status_code)
    if response.status_code in _GONE_STATUSES:
        return ItemAspects(brand=None, mpn=None, oe_part_number=None)
    if response.status_code != 200:
        logger.warning(
            "getItem HTTP %d for %s", response.status_code, ebay_item_id
        )
        return None

    try:
        data = response.json()
    except ValueError:
        logger.warning("getItem non-JSON body for %s", ebay_item_id)
        return None
    if not isinstance(data, dict):
        return ItemAspects(brand=None, mpn=None, oe_part_number=None)

    aspects = {
        a.get("name"): a.get("value")
        for a in data.get("localizedAspects", [])
        if isinstance(a, dict)
    }
    brand = data.get("brand") or aspects.get("Brand")
    return ItemAspects(
        brand=_clip(brand if isinstance(brand, str) else None),
        mpn=_clip(aspects.get("Manufacturer Part Number")),
        oe_part_number=_clip(aspects.get("OE/OEM Part Number")),
    )
```

- [ ] **Step 4: Run tests to verify they pass** — `pytest tests/sources/test_ebay_item.py -v`

- [ ] **Step 5: Lint, type-check, full suite, commit** — `ruff check . && ruff format . && mypy app/ && pytest` then commit: `Add eBay getItem aspects client`

---

### Task 3: Fetch-cycle enrichment

**Files:**
- Modify: `app/worker/fetcher.py` (add function + call at end of `run_fetch_cycle`, before `complete_fetch_run`)
- Modify: `app/db/queries.py` (one helper)
- Test: `tests/worker/test_aspect_enrichment.py`

**Interfaces:**
- Consumes: `fetch_item_aspects`, `ItemAspects` (Task 2); `Listing.aspects_fetched_at` etc. (Task 1).
- Produces: `queries.get_listings_needing_aspects(db, limit) -> list[Listing]`; `enrich_listing_aspects(db, cap=ASPECT_CAP_PER_CYCLE) -> int` in `fetcher.py` — Task 6 documents them.

- [ ] **Step 1: Query helper** — in `app/db/queries.py` (near the other listing queries):

```python
def get_listings_needing_aspects(db: Session, limit: int) -> list[Listing]:
    """Active listings never successfully aspect-enriched, oldest first."""
    return list(
        db.query(Listing)
        .filter(Listing.is_active.is_(True), Listing.aspects_fetched_at.is_(None))
        .order_by(Listing.first_seen_at)
        .limit(limit)
        .all()
    )
```

- [ ] **Step 2: Write failing tests** — `tests/worker/test_aspect_enrichment.py`, monkeypatching `app.worker.fetcher.fetch_item_aspects` (patch the name in the fetcher module's namespace). Cases:
  1. Two pending listings, mock returns `ItemAspects("Dorman", "949-919", "LR124471")` → both rows get brand/mpn/oe persisted and `aspects_fetched_at` stamped; function returns 2.
  2. Mock returns `None` → fields stay NULL, `aspects_fetched_at` stays NULL (retry semantics), return 0.
  3. Mock returns all-None `ItemAspects` → `aspects_fetched_at` stamped (never retried), brand stays NULL.
  4. Cap respected: 3 pending, `cap=2` → mock called exactly twice.
  5. Listing already stamped → mock not called for it.

- [ ] **Step 3: Run tests to verify they fail** — `pytest tests/worker/test_aspect_enrichment.py -v`

- [ ] **Step 4: Implement** — in `app/worker/fetcher.py`:

```python
from app.db.models import utcnow
from app.sources.ebay_item import fetch_item_aspects

ASPECT_CAP_PER_CYCLE = 200


def enrich_listing_aspects(db: Session, cap: int = ASPECT_CAP_PER_CYCLE) -> int:
    """Fetch Item Specifics for listings that never got them. Returns count.

    One getItem call per listing, once ever (transient failures retry on a
    later cycle). Worker-only by design — request handlers never call this.
    """
    pending = queries.get_listings_needing_aspects(db, cap)
    if len(pending) == cap:
        logger.info("Aspect enrichment cap (%d) reached; remainder next cycle", cap)
    enriched = 0
    for listing in pending:
        aspects = fetch_item_aspects(db, listing.ebay_item_id)
        if aspects is None:
            continue
        listing.brand = aspects.brand
        listing.mpn = aspects.mpn
        listing.oe_part_number = aspects.oe_part_number
        listing.aspects_fetched_at = utcnow()
        db.commit()
        enriched += 1
    return enriched
```

In `run_fetch_cycle`, after the `for search in search_list:` loop and before `queries.complete_fetch_run(...)`:

```python
    enriched = enrich_listing_aspects(db)
    if enriched:
        logger.info("Enriched %d listings with Item Specifics", enriched)
```

- [ ] **Step 5: Run tests to verify they pass, full suite, commit** — commit: `Enrich listings with Item Specifics during fetch cycles`

---

### Task 4: Classifier — offbrand verdict + aspect ground truth

**Files:**
- Modify: `app/core/ai_relevance.py`
- Test: `tests/core/test_ai_relevance.py` (extend)

**Interfaces:**
- Consumes: `Listing.brand/mpn/oe_part_number` (Task 1).
- Produces: `VERDICTS = ("part", "accessory", "unrelated", "offbrand")` — Task 5 consumes `"offbrand"` in digest + template.

- [ ] **Step 1: Write failing tests** (extend `tests/core/test_ai_relevance.py`, reusing its existing fake-client fixtures):
  1. `test_offbrand_verdict_accepted`: mock response verdict `offbrand` → mapped into the result dict.
  2. `test_prompt_includes_aspects`: listing with `brand="Dorman"`, `mpn="949-919"`, `oe_part_number="LR124471"` → prompt contains `Brand: Dorman`, `MPN: 949-919`, and `OE#: LR124471`.
  3. `test_prompt_omits_null_aspects`: listing with all aspect fields None → prompt contains neither `Brand:` nor `MPN:` nor `OE#:` on its line.
  4. `test_schema_contains_offbrand`: `"offbrand" in CLASSIFY_SCHEMA` verdict enum.

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement** — in `app/core/ai_relevance.py`:

Change `VERDICTS = ("part", "accessory", "unrelated")` to:

```python
VERDICTS = ("part", "accessory", "unrelated", "offbrand")
```

Replace the `lines = ...` listing-lines construction in `classify_listings` with:

```python
    def _listing_line(i: int, listing: Listing) -> str:
        line = f"{i}. {listing.title} (${listing.price})"
        if listing.brand:
            line += f" — Brand: {listing.brand}"
        if listing.mpn:
            line += f" — MPN: {listing.mpn}"
        if listing.oe_part_number:
            line += f" — OE#: {listing.oe_part_number}"
        return line

    lines = "\n".join(
        _listing_line(i, listing) for i, listing in enumerate(listings)
    )
```

Replace the verdict-definitions block of the prompt (the "For each numbered listing below…" section) with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass, mypy, full suite, commit** — commit: `Add offbrand verdict and Item Specifics ground truth to classifier`

---

### Task 5: Digest demotion + dashboard brand column and alt tag

**Files:**
- Modify: `app/core/digest.py` (one line in `scan_and_record`)
- Modify: `app/web/templates/components/listing_table.html`
- Modify: `app/web/templates/pages/listings.html` (header row, lines 62-68)
- Test: `tests/core/test_digest.py` (extend), `tests/web/test_price_badges.py` (extend)

**Interfaces:**
- Consumes: `"offbrand"` verdict (Task 4), `Listing.brand` (Task 1), existing `listing_extras[id]["relevance"]` plumbing (already populated from `get_relevance_map` — no route change needed).

- [ ] **Step 1: Write failing tests**
  1. In `tests/core/test_digest.py` (copy the existing accessory-demotion test): a new listing whose link has `relevance="offbrand"` is NOT alerted and IS counted in `other_new`.
  2. In `tests/web/test_price_badges.py` (copy `test_relevance_tags_render`): verdict `"offbrand"` renders the `alt` tag with tooltip `Classified as a different brand than this search asks for`.
  3. Brand column: listing with `brand="Dorman"` → page contains `Dorman`; brand None → the cell renders `—`.

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement**

`app/core/digest.py` — in `scan_and_record`, change the notable check to:

```python
                notable = verdict not in (
                    "accessory",
                    "unrelated",
                    "offbrand",
                ) and (
```

`listing_table.html` — after the `unrelated` branch (line 27), add:

```html
            {% elif extras and extras.relevance == 'offbrand' %}
                <span class="badge badge-inactive" title="Classified as a different brand than this search asks for">alt</span>
```

`listing_table.html` — add a Brand cell between the Title cell (ends line 30) and the Price cell (line 31):

```html
    <td>{{ listing.brand or '—' }}</td>
```

`pages/listings.html` — add `<th>Brand</th>` between `<th>Title</th>` (line 63) and `<th>Price</th>` (line 64).

- [ ] **Step 4: Run tests to verify they pass, full suite, commit** — commit: `Demote offbrand from digest alerts; show brand and alt tag on dashboard`

---

### Task 6: Docs + verification

**Files:**
- Modify: `docs/SYSTEM_CONTEXT.md` (changelog + §5 fetch-cycle row)

**Interfaces:**
- Consumes: everything above. EXACT names to use — do not invent others: columns `brand`, `mpn`, `oe_part_number`, `aspects_fetched_at` on `listings`; verdict `offbrand`; module `app/sources/ebay_item.py`; function `enrich_listing_aspects` (cap 200/cycle, provider `ebay_item` in `api_quota_log`).

- [ ] **Step 1: Changelog entry** — add a `2026-08-25` row: brand-aware filtering — Item Specifics (Brand/MPN/OE#) captured once per listing via getItem at fetch time (cap 200/cycle, fail-soft, quota provider `ebay_item`); classifier gains `offbrand` verdict (demoted from notable alerts, `alt` dashboard tag, Brand column); missing/Unbranded never offbrand; rollout includes one-time verdict reset for re-classification.

- [ ] **Step 2: Update the fetch-cycle description** in §5 to mention the enrichment step.

- [ ] **Step 3: Verify docs against code** — every column/function name in the new text must exist verbatim in the diff (`git diff main --stat` + grep). Do NOT describe settings or columns not present in the code.

- [ ] **Step 4: Full suite + lint + commit** — commit: `Update SYSTEM_CONTEXT: brand-aware filtering`

---

### Task 7: Live validation with owner (manual — not for subagents)

1. Merge-gate: final review clean, suite green.
2. Manual `./oemparts fetch --cycle=manual` → backfill ~200 listings; spot-check: auction 267701844481 shows `brand=Dorman`, 358921125862 shows `brand=Unbranded`, `oe_part_number=LR124471`.
3. One-time verdict reset (`UPDATE search_listings SET relevance = NULL, relevance_checked_at = NULL`), manual `./oemparts digest`, audit with owner: Dorman/LR045251 compressors → `offbrand`; "AMK design" + Genuine LR072537 → `part`; unbranded alternator with OE# match → `part`.
4. Overnight soak; then PR after owner sign-off.
