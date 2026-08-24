# Taxonomy-Based Fitment Filtering — Design

**Date:** 2026-08-23
**Status:** Approved by owner (design); spec pending owner review
**Phase:** 2 (replaces the Phase 1 query-enrichment workaround)

---

## 1. Problem

Phase 1 could not use eBay's `compatibility_filter` because it requires a
*leaf-level* fitment-supporting category id. Passing no `category_ids` fails
with eBay error 12504; passing the top-level eBay Motors category (6028) fails
with error 12506 ("category does not support fitment") — both confirmed
against the live API during Phase 1 validation.

The workaround — prepending "YEAR MAKE MODEL" to the query text — has two
costs:

1. **False positives:** listings that merely *mention* the vehicle in the
   title but don't fit it.
2. **False negatives:** listings that fit the vehicle but don't name it in
   the title never match the enriched query at all.

This spec makes searches use a real leaf category plus
`compatibility_filter`, so eBay itself guarantees fitment.

## 2. Goals and non-goals

**Goals**

- Each search automatically resolves an eBay leaf category from its query
  text at creation time (existing searches backfilled via CLI).
- Fetches for category-resolved searches send `category_ids=<leaf>` +
  `compatibility_filter=Year:...;Make:...;Model:...` with the **bare** query
  text (no vehicle prefix).
- Searches that cannot be resolved (or where eBay rejects the filter) fall
  back to exactly today's enriched-query behavior. Degradation is always
  graceful; a fetch never returns nothing *because of* this feature.
- The searches table shows the resolved category name (read-only) so the
  owner can sanity-check the mapping. Unresolved searches show "—".

**Non-goals (explicitly out of scope)**

- Year/Make/Model cascading dropdowns for vehicle creation (separate
  follow-up spec; will reuse the taxonomy client built here).
- Trim/engine-level fitment granularity (Year/Make/Model only).
- Category override UI. Added later only if wrong suggestions actually occur.
- Cron scheduling of `taxonomy-sync` (local-first; run manually for now).
- Full category-tree download (Option B, rejected: heavy, and query→category
  mapping needs the suggestions API regardless).

## 3. Current state (verified 2026-08-23)

- `app/sources/ebay_taxonomy.py` — docstring-only stub, never imported.
- `app/core/compatibility.py` — `build_compatibility_filter()` exists and is
  unit-tested but is **dead code** (no production caller). It joins with
  commas; eBay's documented format is semicolon-separated. Fix here.
- `app/sources/ebay_browse.py` — `search_ebay()` accepts
  `compatibility_filter` but no `category_ids` param; when a filter is
  passed it hardcodes `category_ids="6028"` — the exact value proven to
  fail. Branch is unreachable in production today.
- `app/core/search_runner.py:48-62` — the enrichment workaround; the single
  plug-in point for the new call shape.
- `app/db/models.py` — `taxonomy_cache` table exists, never read or
  written. `searches` has **no** category column. `listings` has a
  `compatibility_checked` boolean that is never set.
- `app/worker/cli.py` — no `taxonomy-sync` subcommand (docs mention one).
- `api_quota_log.provider` already anticipates an `ebay_taxonomy` value.

## 4. eBay Taxonomy API (reference)

Base URL `https://api.ebay.com/commerce/taxonomy/v1` (sandbox:
`api.sandbox.ebay.com`, selected the same way `ebay_browse` does). Auth: the
existing client-credentials token from `ebay_oauth.py` (base
`api_scope` covers Taxonomy). Three endpoints:

| Endpoint | Purpose | Caching |
|----------|---------|---------|
| `GET /get_default_category_tree_id?marketplace_id=EBAY_MOTORS_US` (Motors tree, required for fitment categories — live-validation correction 2026-08-24) | The Motors category-tree id (a small static value) | In-process, like the OAuth token |
| `GET /category_tree/{tree_id}/get_category_suggestions?q=<query>` | Ranked category suggestions for query text; each suggestion carries the category (id + name) and its ancestor path | Not cached — called once per search at creation/backfill; the result persisted on the search row *is* the cache |
| `GET /category_tree/{tree_id}/get_compatibility_properties?category_id=<id>` | Which compatibility properties (Year/Make/Model/…) a category supports; an error or empty list means "no fitment support" | `taxonomy_cache` table, keyed (category_id, marketplace), response JSON in `raw_json`, `refreshed_at` for TTL |

Every call is logged to `api_quota_log` with provider `ebay_taxonomy`.
Cost per new search: 1–2 calls (suggestions + at most a few
compatibility-property checks, most of which will hit the cache).

**Implementation note:** the exact response shapes and the
semicolon-vs-comma `compatibility_filter` separator must be verified against
the live API early in implementation (Phase 1 never exercised these paths;
docs and code currently disagree on the separator). eBay's documented Browse
format is `compatibility_filter=Year:2012;Make:Land Rover;Model:LR4`.

## 5. Design

### 5.1 Schema (one new Alembic migration)

Add to `searches`:

- `category_id` — `String(20)`, nullable. The resolved eBay leaf category.
- `category_name` — `String(100)`, nullable. Display name for the UI.

`NULL` category_id is the fallback signal: such a search fetches exactly as
today. Pure DDL migration — no data backfill inside the migration (backfill
is the CLI's job, since it needs live API calls).

`taxonomy_cache` is reused as-is; no schema change. Rows written by the
compatibility-properties lookup use `category_id`, `marketplace`,
`raw_json`, `refreshed_at`; the `year/make/model/trim` columns stay NULL
(they belong to the future dropdowns feature).

### 5.2 Taxonomy client — `app/sources/ebay_taxonomy.py`

Mirrors `ebay_browse.py` conventions (httpx sync, env-based URL selection,
quota logging, no secrets logged):

```python
def get_default_tree_id(db: Session) -> str
def get_category_suggestions(db: Session, query: str) -> list[CategorySuggestion]
def get_compatibility_properties(db: Session, category_id: str) -> list[str]
```

`CategorySuggestion` is a small dataclass: `category_id`, `category_name`,
`ancestor_ids: list[str]` (root-to-leaf). `get_compatibility_properties`
returns the property names (e.g. `["Year", "Make", "Model", "Trim"]`),
reading through `taxonomy_cache` first; an eBay error response for an
unsupported category is treated as "no fitment support" and cached too
(cached-empty), so repeat lookups don't re-hit the API. Cache TTL: 30 days
(category fitment support changes rarely); `refreshed_at` governs.

### 5.3 Category resolution — `app/core/compatibility.py`

```python
def resolve_fitment_category(db: Session, query_text: str) -> ResolvedCategory | None
```

Walk eBay's suggestions in ranked order; return the first whose

1. ancestor path contains `6028` (eBay Motors Parts & Accessories), and
2. compatibility properties include Year, Make, and Model.

Return `None` when no suggestion qualifies (e.g. a query like "OBD scanner"
that maps outside Motors). Any API/network error → log a warning, return
`None` — resolution failure must never propagate.

Also in this module: `build_compatibility_filter()` separator fixed from
`,` to `;` (existing unit tests updated). The eBay Motors root constant
(`EBAY_MOTORS_PARTS_CATEGORY_ID = "6028"`) lives here, in code — not in
`.env` — since it is a marketplace fact, not configuration.

### 5.4 Resolution triggers

- **Search creation (web route):** call `resolve_fitment_category` inline —
  permitted by the project's request-handler exception for taxonomy lookups.
  Store id + name on the new row. On `None` or error, create the search
  without a category (fallback mode) — creation never blocks or fails
  because of taxonomy.
- **`oemparts taxonomy-sync` (new CLI subcommand):** resolves categories for
  active searches that have none (the backfill for existing rows); the
  `--all` flag re-resolves every active search (for when eBay's tree
  changes or query text was edited). Prints a per-search result table:
  resolved name or "no fitment category".
- **Query-text edits:** not applicable — the dashboard has no search-edit
  route (verified: `searches.py` exposes create/toggle/fetch/delete only).
  If an edit flow is ever added, its handler must re-run resolution.

### 5.5 Fetch path — `search_runner.py` + `ebay_browse.py`

`search_ebay()` changes:

- New parameter `category_ids: str | None = None`, passed through verbatim.
  The hardcoded-6028 branch is deleted; `compatibility_filter` and
  `category_ids` become independent, caller-controlled params.
- Listings fetched under a compatibility filter parse eBay's per-item
  `compatibilityMatch` field when present; `NormalizedListing` gains an
  optional `compatibility_match` value (parsed, not yet persisted beyond
  the flag below).

`run_single_search()` branches:

```
if search.category_id:                      # fitment mode
    query   = search.query_text             # bare — no vehicle prefix
    filter  = build_compatibility_filter(vehicle.year, vehicle.make, vehicle.model)
    listings = search_ebay(db, query, compatibility_filter=filter,
                           category_ids=search.category_id, ...)
else:                                       # fallback mode (today's behavior)
    query   = f"{vehicle.year} {vehicle.make} {vehicle.model} {search.query_text}"
    listings = search_ebay(db, query, ...)
```

**Error fallback:** if a fitment-mode call fails with a category/fitment
error (eBay 12xxx family), log a warning naming the search and category,
then retry once in fallback mode within the same run. The stored
category is left in place (a transient eBay problem shouldn't erase it);
persistent failures surface in logs and can be re-resolved via
`taxonomy-sync --all`.

**Persistence:** listings fetched in fitment mode set
`listings.compatibility_checked = True` (both insert and update paths in
`queries.upsert_listing`). The OEM-only post-filter is untouched and still
runs after fetching in both modes.

Stale docstrings in `search_runner` and `ebay_browse` are corrected as part
of this work.

### 5.6 Deduplication — no change required (verified during planning)

SYSTEM_CONTEXT §5 describes cross-user fetch sharing keyed on normalized
query + vehicle, but the actual `deduplicator.py` implements only a
per-search TTL check on `last_fetched_at` (`should_skip_search`) — there is
no cross-search cache key and no fetch sharing between searches. A skipped
search reuses its *own* prior results, which remain correct under this
feature. Therefore no deduplicator change is needed. If cross-search fetch
sharing is ever actually implemented, its key must include `category_id`
(including the both-NULL case) so fitment-filtered and fallback result sets
can never be served interchangeably.

### 5.7 UI

Searches table gets one read-only column, **Category**: shows
`category_name`, or "—" for fallback-mode searches (tooltip: "No eBay
fitment category — using vehicle keywords in the query"). No edit control.
Template + route-context change only; HTMX partials for search rows updated
accordingly.

## 6. Testing

All against the dedicated `oemparts_test` database, mocked eBay responses
via httpx mocking (no live calls in the suite):

1. **Taxonomy client:** suggestion parsing (incl. ancestor path),
   compatibility-properties parsing, cache hit vs miss vs expired TTL,
   cached-empty for unsupported categories, quota-log rows written.
2. **Resolution:** picks first qualifying suggestion; skips non-Motors and
   non-fitment suggestions; returns None on no match; returns None (and
   logs) on API error.
3. **Filter builder:** semicolon format (existing tests updated).
4. **Search runner:** fitment mode sends bare query + category_ids + filter;
   fallback mode sends enriched query and no filter; 12xxx error triggers
   one fallback retry; `compatibility_checked` set only in fitment mode.
5. **Deduplicator:** no new tests — no change is made (see §5.6).
6. **Migration:** upgrade → columns exist and are NULL; downgrade clean.
7. **CLI:** `taxonomy-sync` resolves missing categories, `--all` re-resolves,
   output table correct (resolution mocked).

**Live validation (manual, after implementation):** run `taxonomy-sync`
against the real LR4 searches; verify each resolved category looks sane in
the dashboard; run a manual fetch cycle; compare listing counts and result
quality against the pre-change behavior; confirm the semicolon filter format
is accepted by the production API. This mirrors Phase 1's real-data
validation.

## 7. Rollout

1. Migration + code merge (searches keep NULL categories → behavior
   unchanged everywhere).
2. `oemparts taxonomy-sync` to backfill the owner's searches.
3. Manual fetch + dashboard inspection (live validation above).
4. SYSTEM_CONTEXT.md changelog + §5/§15 updates when shipped.

Risk is bounded at every step: NULL category = today's behavior, and the
runtime error fallback catches bad categories per-fetch.
