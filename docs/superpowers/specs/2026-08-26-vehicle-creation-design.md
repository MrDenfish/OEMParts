# Vehicle Creation Overhaul (VIN Decode + Y/M/M Dropdowns) — Design

**Date:** 2026-08-26
**Status:** Spec pending owner review
**Phase:** 2 (the two remaining Phase 2 features, combined: they share one API
client and one form)

---

## 1. Problem

Vehicle creation is three free-text fields. Typos ("Land Rover" vs
"LandRover" vs "Range Rover") silently break fitment filtering, because the
`compatibility_filter` sends Make/Model verbatim to eBay. And owners of a
real car usually have its VIN on the registration — typing year/make/model
by hand is busywork the NHTSA vPIC API can do authoritatively.

Phase 1 already scaffolded for this: `vehicles.vin` column,
`vin_decode_cache` table (both in the initial migration), and
`nhtsa_vpic_base_url` in config (`https://vpic.nhtsa.dot.gov/api`).
`app/sources/nhtsa_vpic.py` is a 5-line stub. Nothing is wired.

## 2. Goals and non-goals

**Goals**

- **VIN fast path:** paste a 17-char VIN → Decode button (HTMX) →
  year/make/model/trim auto-filled, VIN stored on the vehicle.
- **Cascading dropdowns:** Year (static 1981–2027), Make (curated ~60
  consumer makes, shipped in code), Model (HTMX-loaded from NHTSA for the
  chosen make+year, cached).
- **Free-text fallback:** a "my model isn't listed" escape hatch that
  reveals the current text inputs — NHTSA gaps must never block creation.
- **Fail-soft:** NHTSA outage degrades to free-text, never a broken form.
- **Caching:** VIN decodes cached permanently (`vin_decode_cache`, exists);
  model lists cached in a new table; inline calls in request handlers are
  the sanctioned sub-second exception (same as taxonomy lookups).
- **No VINs in logs** (house rule): log first/last 4 chars at most.

**Non-goals (explicitly out of scope)**

- Trim/engine dropdowns (NHTSA trim data is too sparse; trim stays
  free-text, best-effort filled by VIN decode).
- Vehicle *editing* (today's model is create/delete; unchanged).
- Backfilling VINs or re-validating existing vehicles (the owner's 2012
  LR4 rows stay as they are).
- Using NHTSA's full GetAllMakes registry (10,000+ trailer/coachbuilder
  entries — a garbage dropdown; curated list instead, owner-approved).
- eBay taxonomy-based Y/M/M (eBay's compatibility metadata varies by
  category; NHTSA is the vehicle authority — categories stay eBay's job).

## 3. Current state (verified 2026-08-26)

- `app/web/routes/vehicles.py::create_vehicle` takes `year/make/model/
  trim/nickname` Form fields, strips, creates, returns an HTMX row
  partial (`components/vehicle_row.html`) or 303.
- `pages/vehicles.html` form: `hx-post="/vehicles"` with number/text
  inputs; no `vin` field posted today.
- `queries.create_vehicle` does not accept a `vin` argument yet.
- `vin_decode_cache`: `vin(PK), year, make, model, trim, body_class,
  raw_json, decoded_at` — in the initial migration, never written to.
- No test file for the NHTSA client; `tests/web/` has vehicle-page tests
  to extend.

## 4. Design

### 4.1 NHTSA client — `app/sources/nhtsa_vpic.py` (replaces the stub)

No new dependency (httpx, stdlib). No auth, no key. Endpoints:

- Decode: `GET {base}/vehicles/DecodeVinValues/{vin}?format=json` —
  flat single-row response; fields `ModelYear`, `Make`, `Model`, `Trim`,
  `BodyClass`, `ErrorCode` (0 or "0" = clean decode; codes 1-13 range
  from warnings to garbage — treat any non-clean `ErrorCode` that still
  yields year+make+model as usable, log the code).
- Models: `GET {base}/vehicles/GetModelsForMakeYear/make/{make}/
  modelyear/{year}?format=json` — `Results[].Model_Name`.

```python
@dataclass
class DecodedVin:
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    body_class: str | None

def decode_vin(db: Session, vin: str) -> DecodedVin | None
def get_models_for_make_year(db: Session, make: str, year: int) -> list[str] | None
```

- `decode_vin`: normalize (upper, strip); validate 17 chars alphanumeric
  excluding I/O/Q before any network call (invalid → None, no API hit).
  Check `vin_decode_cache` first; on API success (even partial) write the
  cache row with `raw_json` and return; on HTTP/network error → warning
  log (VIN redacted to `XXXX…XXXX` first/last 4) and None. A decode whose
  year/make/model are all empty caches as an all-None row (final: NHTSA
  doesn't know this VIN; don't re-ask).
- `get_models_for_make_year`: cache first, then API; on error → None
  (fail-soft: the form falls back to free text). Results sorted
  alphabetically, deduplicated.
- NHTSA is a free public API with no published hard quota; calls are NOT
  logged to `api_quota_log` (that table is eBay-scoped observability).
  Timeout 10s.

### 4.2 Model-list cache (one new Alembic migration)

New table `nhtsa_model_cache`:

- `make` — `String(100)`, PK part
- `year` — `Integer`, PK part
- `models_json` — `Text`, JSON array of model names
- `cached_at` — `TIMESTAMPTZ`, default utcnow

TTL 30 days: rows older than that are refreshed on next request (model
lists for a past year barely change; a stale list is still useful if
refresh fails — serve stale on API error). Composite PK (make, year).

### 4.3 Curated makes — `app/web/makes.py` (new, code not DB)

`COMMON_MAKES: tuple[str, ...]` ≈ 60 consumer brands sold in the US
(Acura … Volvo, including Land Rover, Rover, MINI, defunct-but-common
Pontiac/Saturn/Mercury/Oldsmobile/Plymouth/Suzuki/Isuzu/Scion/Fisker,
and truck brands RAM/GMC/Hummer). Values must match NHTSA's make naming
(NHTSA is case-insensitive on the URL path). Alphabetical in the UI.

### 4.4 Routes — `app/web/routes/vehicles.py`

- `POST /vehicles/decode-vin` (HTMX): Form field `vin`. Calls
  `decode_vin`. Success → returns a form-fields partial with
  year/make/model/trim filled (make/model as text inputs in this state —
  decoded values are authoritative, dropdowns unnecessary) plus the VIN
  in a hidden field. Failure/unknown VIN → same partial, empty fields,
  and an inline message "Could not decode that VIN — pick or type the
  details instead." Never a 500.
- `GET /vehicles/models` (HTMX): query params `make`, `year`. Returns an
  `<option>` list partial from `get_models_for_make_year`; on None or
  empty, returns the fallback option `<option value="">(not listed —
  type it below)</option>` and the page reveals the free-text input.
- `POST /vehicles` gains optional `vin: str | None = Form(None)`;
  `queries.create_vehicle` gains `vin: str | None = None` passthrough.
  Whichever path filled the fields, creation is the same handler.
- Both new handlers are inline-external-call exceptions per CLAUDE.md
  (sub-second, cached), like taxonomy resolution at search creation.

### 4.5 Form — `pages/vehicles.html` + partials

Layout (top to bottom):

1. **VIN row:** text input (maxlength 17) + "Decode VIN" button
   (`hx-post="/vehicles/decode-vin"`, targets the fields container).
2. **Fields container** (swapped by decode; default state below):
   - Year: `<select>` 2027→1981 (rendered server-side, no JS).
   - Make: `<select>` of `COMMON_MAKES` with
     `hx-get="/vehicles/models" hx-include="[name=year],[name=make]"
     hx-target="#model-select"` (also triggered by Year change).
   - Model: `<select id="model-select">` (starts disabled, "pick year +
     make first"); the "(not listed)" option toggles a free-text model
     input via a small inline `hx-on` / checkbox reveal — no build step,
     no custom JS files.
   - Trim, Nickname: unchanged free-text.
3. Existing submit → unchanged `hx-post="/vehicles"`.

The free-text fallback keeps `make` as text too ("Other make…" option at
the bottom of the Make select reveals both text inputs), so no vehicle is
impossible to enter.

### 4.6 What it feeds

Nothing downstream changes: `compatibility_filter` and fitment already
consume `vehicle.year/make/model`. Better inputs → fewer silent fitment
misses. Decoded `vin` is stored on the vehicle row (String(17) exists).

## 5. Testing

All NHTSA calls mocked (monkeypatch `httpx.get` in `nhtsa_vpic`'s
namespace, same pattern as `ebay_item`):

1. Client: VIN validation rejects bad lengths/chars pre-network; clean
   decode parses fields; ErrorCode-with-usable-data still returns;
   cache hit skips the API; cache write on success; all-empty decode
   caches all-None (no re-ask); network error → None + redacted log
   (assert full VIN NOT in caplog).
2. Models: cache hit/miss/expiry (TTL 30d), serve-stale-on-error, sorted
   + deduped, None on error.
3. Routes: decode-vin partial filled/failure states; models options
   partial + fallback option; create_vehicle stores vin; free-text path
   still creates (regression).
4. Migration: nhtsa_model_cache round-trip.

**Live validation (with owner):** owner pastes their real LR4 VIN →
fields fill (expect 2012 / Land Rover / LR4) → create → verify fitment
searches still resolve; dropdown path: 2012 + Land Rover → models list
contains LR4/Range Rover/etc.; NHTSA-down path simulated by temporarily
pointing base_url at an invalid host → form still usable via free text.

## 6. Rollout

1. Migration + code merge — purely additive; existing vehicles and the
   free-text path untouched.
2. Owner live-validates (VIN + dropdown + fallback paths).
3. SYSTEM_CONTEXT changelog; Phase 2 feature list complete.

Risk bounded: fail-soft on every NHTSA path, free-text fallback always
available, no new dependency, no schedule/worker changes, VINs redacted
in logs.
