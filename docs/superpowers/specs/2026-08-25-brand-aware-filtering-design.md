# Brand-Aware Filtering — Design

**Date:** 2026-08-25
**Status:** Spec pending owner review
**Phase:** 2/3 bridge (follows the AI relevance filter, PR #5)

---

## 1. Problem

Title text lies about brand. Live example from the owner (2026-08-25):
auction 267701844481 titles itself "…Amk Air Ride Compressor" but its eBay
Item Specifics say `Brand: Dorman`, `MPN: 949-919`. The seller put "AMK" in
the title to catch brand searches. Verified via a live `getItem` probe: the
structured aspects expose the truth, but our fetch uses the Browse *search*
endpoint, whose item summaries do not include brand at all — so today the
database has no brand data and every filter (OEM-only, AI classifier) works
from the title alone.

A second, related gap: the AI classifier's `part` verdict says "this is the
part the search is about" but is brand-blind — a Dorman compressor is
genuinely a compressor, so it classifies `part` even when the query
explicitly asks for AMK.

## 2. Goals and non-goals

**Goals**

- **Capture ground truth:** store each listing's `Brand` and `Manufacturer
  Part Number` from eBay's `getItem` endpoint — one call per listing,
  once ever, quota-logged, fail-soft.
- **Give the AI the truth:** include Brand/MPN lines in the classification
  prompt so verdicts stop depending on title honesty.
- **New verdict `offbrand`:** a functional substitute from a different
  brand than one named in the search query (Dorman vs AMK). Demoted from
  "notable" digest alerts exactly like `accessory`/`unrelated`; tagged on
  the dashboard for audit. Genuine/OE items are never `offbrand` (the
  Genuine Land Rover LR072537 assembly IS the AMK-built part). Queries
  that name no brand never produce `offbrand`.
- **Show the brand:** dashboard listings display the stored brand so
  title deception is visible at a glance.
- **One-time re-classification:** after brand backfill, reset all existing
  verdicts to NULL so the daily pass re-classifies with brand data
  (~11 Claude calls, ≈$0.20 one-time).

**Non-goals (explicitly out of scope)**

- Server-side `aspect_filter` at fetch time (silent and blunt; rejected in
  the owner's Option A/B decision — everything stays auditable instead).
- A per-search accepted-brands list or any new search-creation UI field
  (the AI reads brands from the query text the owner already writes).
- Retrying aspect lookups for listings whose seller left Brand blank
  (looked-up-and-empty is a final state).
- Filtering or hiding offbrand listings in the dashboard (tags only,
  same policy as the existing `acc`/`off` tags).
- Deleting the five stale AMK-search links that matched while OEM-only
  was toggled off — the re-classification pass will tag/demote them
  instead, which keeps the audit trail.

## 3. Current state (verified 2026-08-25)

- `listings` has no brand/MPN columns (`app/db/models.py:167`).
- `NormalizedListing` (`app/sources/ebay_browse.py:38`) carries only
  search-summary fields; `upsert_listing` (`app/db/queries.py:240`)
  persists them.
- `app/core/ai_relevance.py`: `VERDICTS = ("part", "accessory",
  "unrelated")`; prompt sends numbered titles + prices; `String(10)`
  on `search_listings.relevance` fits `offbrand` (8 chars) — **no
  migration needed on the junction table**, only on `listings`.
- Digest demotion: `verdict not in ("accessory", "unrelated")` in
  `app/core/digest.py::scan_and_record`.
- Dashboard tags: `acc`/`off` in
  `app/web/templates/components/listing_table.html` via
  `listing_extras[id]["relevance"]`.
- eBay quota: 5,000 calls/day; every call logged to `api_quota_log`.
  Live probe confirmed `getItem` returns `brand` (top-level) and
  `localizedAspects` (Brand, Manufacturer Part Number) with the
  standard Browse OAuth token.

## 4. Design

### 4.1 Schema (one new Alembic migration, `listings` only)

- `brand` — `String(100)`, nullable. From `getItem` top-level `brand`,
  falling back to the `Brand` localized aspect.
- `mpn` — `String(100)`, nullable. From the `Manufacturer Part Number`
  aspect.
- `aspects_fetched_at` — `TIMESTAMPTZ`, nullable. Stamped when a lookup
  **succeeds** (even if the seller left Brand blank). NULL = not yet
  looked up (or last attempt failed → retried next cycle).

### 4.2 Item detail client — `app/sources/ebay_item.py` (new)

```python
@dataclass
class ItemAspects:
    brand: str | None
    mpn: str | None

def fetch_item_aspects(db: Session, ebay_item_id: str) -> ItemAspects | None
```

- `GET https://api.ebay.com/buy/browse/v1/item/v1|{ebay_item_id}|0`
  (sandbox variant mirrors `_get_browse_url`), reusing `get_ebay_token`
  and the standard headers.
- Logged to `api_quota_log` with provider `ebay_item` (same pattern as
  `_log_api_call` in `ebay_browse.py`).
- Fail-soft: any HTTP error, timeout, or surprise shape → log warning,
  return `None`; the listing keeps `aspects_fetched_at NULL` and is
  retried on a later cycle. A 404 (listing ended between search and
  lookup) is treated as success-with-nothing: stamp the timestamp so it
  is never retried.

### 4.3 Fetch-cycle enrichment — worker only

At the end of each fetch cycle (`run_fetch_cycle`), after upserts:

1. Select listings with `aspects_fetched_at IS NULL` and `is_active`,
   oldest `first_seen_at` first, capped at **200 per cycle** (cap logged
   when hit). The first nightly run after deploy drains the ~200-listing
   backlog; steady state is a handful of new listings per day.
2. For each, one `fetch_item_aspects` call; on success write
   `brand`/`mpn`/`aspects_fetched_at` and commit per listing.

Request handlers never call this — worker/cron only, per the standing
architecture rule.

### 4.4 AI classifier changes — `app/core/ai_relevance.py`

- `VERDICTS = ("part", "accessory", "unrelated", "offbrand")`; the
  json_schema enum gains `offbrand`.
- Each numbered listing line gains structured-truth suffixes when known:
  `— Brand: Dorman — MPN: 949-919`.
- Prompt instruction added: *if the search query names a brand or
  manufacturer, a functionally equivalent item from a different brand is
  `offbrand`; trust the Brand field over words in the title; genuine/OE
  branded items for the vehicle are `part`, not `offbrand`; if the query
  names no brand, never use `offbrand`.*
- `craft_query` unchanged.

### 4.5 Digest and dashboard

- Demotion tuple becomes `("accessory", "unrelated", "offbrand")` in
  `scan_and_record` — offbrand new listings count in "other new" totals,
  never "notable".
- Dashboard: `offbrand` renders an `alt` tag (tooltip: "Classified as a
  different brand than this search asks for"), same subdued styling as
  `acc`/`off`.
- Listings table gains a Brand column (blank when unknown) so the stored
  truth is visible next to the title.

### 4.6 One-time rollout data pass

After the enrichment backfill has run (first nightly cycle, or a manual
`./oemparts fetch`), reset all junction verdicts once:
`UPDATE search_listings SET relevance = NULL, relevance_checked_at = NULL`.
The next digest re-classifies everything with brand data (~11 calls,
≈$0.20). Documented as a rollout step, not code.

## 5. Testing

All external calls mocked; suite stays hermetic:

1. `ebay_item`: aspect extraction (top-level brand + aspect fallback),
   404-stamps-timestamp, error-returns-None-and-retries, quota logging.
2. Enrichment: cap respected; failure leaves `aspects_fetched_at` NULL;
   success persists brand/mpn; already-fetched listings skipped.
3. Classifier: prompt contains Brand/MPN lines when present and omits
   them when NULL; `offbrand` accepted from the schema and persisted.
4. Digest: `offbrand` demoted from notable, counted in other-new;
   regression pair with NULL/part unchanged.
5. UI: `alt` tag + Brand column render from data.

**Live validation (with owner):** run a manual fetch to backfill aspects →
verify auction 267701844481 shows `Brand: Dorman` → reset verdicts → manual
digest → the Dorman and the LR045251 generics on the AMK search should come
back `offbrand`, the "AMK design" items and Genuine LR072537 assemblies
`part` → next morning's digest demotes offbrand items.

## 6. Rollout

1. Migration + code merge (enrichment starts on the next fetch cycle;
   verdicts unchanged until the reset).
2. Backfill via nightly or manual fetch; spot-check brands.
3. One-time verdict reset; manual digest; audit verdicts with owner.
4. Overnight soak; SYSTEM_CONTEXT changelog updated with the feature.

Risk bounded: brand columns are additive and nullable; aspect lookups are
fail-soft and worker-only; `offbrand` inherits the existing fail-open
contract (NULL = today's behavior); eBay quota impact is ~200 calls once,
then negligible.
