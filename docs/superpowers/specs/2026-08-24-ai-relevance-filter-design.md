# AI Relevance Filter — Design

**Date:** 2026-08-24
**Status:** Spec pending owner review
**Phase:** 2/3 bridge (owner-proposed 2026-08-24; cost-conscious design per that discussion)

---

## 1. Problem

Fitment filtering guarantees a listing *fits the vehicle*; nothing guarantees
it *is the part*. Live example from the first digest email (2026-08-24): the
"AMK air suspension compressor" search flagged eBay item 286828778103 — a $25
**bracket mount kit** — as "among the cheapest in this search". The statement
was true and useless: accessories (brackets, relays, pipes, mounts) match the
search words, fit the vehicle, and are naturally the cheapest things in the
result set, so they pollute exactly the signal meant to surface deals.

Deterministic fixes are blunt: dropping the low-price signal for OEM-numbered
searches would also hide genuinely cheap *actual* compressors whose titles
omit the part number. Distinguishing "the part" from "a thing for the part"
from a title is a judgment call — which is what an LLM is for. The owner
proposed this shape explicitly: AI at search creation plus one cheap pass per
day, never per-fetch.

## 2. Goals and non-goals

**Goals**

- **Daily relevance pass** (the value driver): classify each (search,
  listing) pair as `part` / `accessory` / `unrelated`, once per listing per
  search, batched into one API call per search per day, run as a step inside
  the existing digest job (no new schedule).
- **Digest integration:** listings classified `accessory`/`unrelated` are
  excluded from "notable new" alerts (they still count in the "other new"
  totals). Unclassified listings behave exactly as today — **fail open**: an
  API outage degrades to the current behavior, never blocks the digest.
- **Dashboard integration:** a subdued tag on classified-away listings
  (`acc` / `off`) so the owner can audit the classifier's calls.
- **Creation-time query crafting** (secondary, severable): when a search is
  created, the model may refine the query text (brand words, part noun,
  no connector words — the lessons learned manually on 2026-08-24). Applied
  best-effort; any failure keeps the user's text untouched.
- Hard cost ceiling by construction: at most one classification call per
  search per day plus one call per search creation.
- Kill switch: `AI_FILTER_ENABLED` (default **false**) — the feature is
  inert until an API key is configured.

**Non-goals (explicitly out of scope)**

- Per-fetch or real-time classification (cost discipline).
- Classifying historical inactive listings (active ones only).
- Message Batches API (halves cost but adds polling machinery — "boring
  tech" wins at ~12 calls/day; revisit only if volume grows 10×).
- Re-classification sweeps (a verdict is permanent unless the search's
  query/OEM changes — see §4.4).
- Any UI for correcting verdicts (audit tags only; corrections are a future
  feature if misclassifications actually annoy).

## 3. Current state (verified 2026-08-24)

- `search_listings` junction: (search_id, listing_id, matched_at) — the
  natural home for a per-(search, listing) verdict. **This feature needs a
  migration** (two columns) — the first since `120604f077fb`.
- `app/core/digest.py::scan_and_record` — the "notable" decision point the
  filter plugs into; `run_digest` orchestrates per-user work daily at 07:00.
- Search creation route (`app/web/routes/searches.py::create_search`)
  already does an inline external lookup (category resolution) under the
  sanctioned request-handler exception; query crafting slots beside it.
- No `anthropic` dependency; no `ANTHROPIC_API_KEY` in config.

## 4. Design

### 4.1 Schema (one new Alembic migration)

Add to `search_listings`:

- `relevance` — `String(10)`, nullable. `part` / `accessory` / `unrelated`;
  NULL = not yet classified.
- `relevance_checked_at` — `TIMESTAMPTZ`, nullable.

NULL relevance always means "behave as today" — the fail-open contract.
Pure DDL, no backfill.

### 4.2 AI client — `app/core/ai_relevance.py` (new)

New dependency: `anthropic` SDK (justified: first-party client, retries and
typed errors built in; raw httpx would reimplement it). New settings:

```bash
AI_FILTER_ENABLED=false
ANTHROPIC_API_KEY=
AI_MODEL=claude-opus-5          # swap to claude-haiku-4-5 to cut cost ~5x
```

One public function per touchpoint:

```python
def classify_listings(search: Search, listings: list[Listing]) -> dict[uuid.UUID, str] | None
def craft_query(query_text: str, oem_number: str | None, vehicle: Vehicle,
                category_name: str | None) -> str | None
```

`classify_listings` sends ONE request per search: a system prompt stating
the task, the search's intent block (query text, OEM number, category,
vehicle Y/M/M), and the numbered listing titles (+ prices). It uses
**structured outputs** (`output_config.format` json_schema: an array of
`{index, verdict}` with `verdict` enum-locked to the three values) so the
response needs no parsing heuristics. `max_tokens` sized to the batch;
temperature not set (not accepted on current models). Any API error, refusal,
or schema surprise → log warning, return `None` (fail open). The API key is
never logged; prompt content is public eBay data plus the search definition.

`craft_query` is a single small call returning a refined query string (or
None on any failure). The prompt encodes the empirically learned rules:
keep brand/manufacturer words, include the part noun, drop connector words
("to", "and"), never invent part numbers.

**Cost reality check** (current API pricing, owner's scale): ~12 searches ×
~40 titles ≈ 25–30K input + ~2K output tokens per day. Claude Opus 5
($5/$25 per MTok): ≈ **$0.20/day ≈ $6/mo**. Claude Haiku 4.5 ($1/$5):
≈ **$0.04/day ≈ $1.20/mo**. Either is acceptable; the default follows
current API guidance (Opus) and the env var makes the cheap swap a
one-line change the owner can make at any time.

### 4.3 Daily pass — step 0 of `run_digest`

Before `scan_and_record`, for each user with active searches (skipped
entirely when `AI_FILTER_ENABLED` is false or the key is empty):

1. For each active search: collect linked **active** listings with
   `relevance IS NULL`, capped at **100 per search per run** (cap logged
   when hit; the remainder classifies tomorrow).
2. One `classify_listings` call; write verdicts + `relevance_checked_at`
   on the junction rows; commit per search.
3. On `None` (failure): leave NULL, continue — tomorrow retries.

`scan_and_record`'s notable check gains one condition: a new listing is
notable only if its junction `relevance` is `part` **or NULL**. Alert
*creation* is unchanged otherwise; dedup and retry semantics untouched.
Existing unclassified backlog (the ~250 current listings) classifies over
the first days via the daily cap — no special backfill.

### 4.4 Verdict lifecycle

A verdict is per (search, listing) and permanent — cheap and predictable.
Exception: the future search-edit feature must reset `relevance` to NULL
for all of a search's links when query text or OEM number changes (noted
here as a contract for that feature; today searches are delete/recreate,
and recreation naturally starts unclassified).

### 4.5 Creation-time crafting — `create_search` route

After OEM/condition parsing and before category resolution: if AI is
enabled, call `craft_query`; on a non-None result, use it as the stored
`query_text` (category resolution then runs on the refined text, which is
the point — better queries resolve better categories). The searches table
already displays query text, so the refinement is visible immediately.
Failure or disabled → user's text verbatim. This section is **severable**:
if the owner strikes it at review, the daily pass stands alone.

### 4.6 Dashboard

Listings rows: for the currently filtered search, junction rows with
`relevance='accessory'` render a subdued `acc` tag, `unrelated` an `off`
tag (tooltips: "Classified as an accessory for this search" / "Classified
as unrelated to this search"). No filtering/hiding in this iteration —
audit visibility only.

## 5. Testing

Anthropic client fully mocked (monkeypatch the module's client factory);
suite stays hermetic and key-free:

1. `classify_listings`: request shape (one call, titles numbered, schema
   attached), verdict mapping, failure → None, refusal/stop_reason → None.
2. Daily pass: verdicts persisted with timestamps; cap respected; failure
   leaves NULL and digest still runs; disabled flag skips entirely.
3. Notable integration: `accessory`/`unrelated` excluded from alerts but
   counted in other-new; NULL behaves exactly as pre-feature (regression
   pair: same fixture with and without verdicts).
4. `craft_query`: refined text stored; None keeps user text; category
   resolution receives the refined text.
5. UI: tags render from junction data.

**Live validation (with owner):** owner creates an Anthropic API key
(console.anthropic.com — separate billing from the Claude subscription) →
`.env` → manual `oemparts digest` run → inspect verdicts on the AMK
compressor search (brackets should classify `accessory`; the URO/AMK/
Hitachi compressors `part`) → next morning's digest should flag compressors
only. Cost check after a week against the estimate.

## 6. Rollout

1. Migration + code merge (inert: flag false, no key).
2. Owner adds API key + flips flag; manual run; verdict audit on real data.
3. Overnight soak; cost sanity check.
4. SYSTEM_CONTEXT changelog + docs updates when shipped.

Risk bounded: fail-open everywhere, NULL = today's behavior, one flag to
turn it all off, verdicts auditable in the dashboard before trusting them.
