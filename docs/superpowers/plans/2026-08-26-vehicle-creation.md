# Vehicle Creation Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** VIN decode (NHTSA vPIC, cached) plus cascading Year/Make/Model dropdowns with a free-text fallback on the vehicle-creation form.

**Architecture:** A real NHTSA client replaces the Phase-1 stub, caching VIN decodes permanently (`vin_decode_cache`, exists) and model lists 30 days (new `nhtsa_model_cache`). Two HTMX endpoints (`POST /vehicles/decode-vin`, `GET /vehicles/models`) drive a reworked form whose fields live in a swappable partial with two states (dropdown / decoded). Free-text inputs are always present and override the selects server-side — no JS files, no build step.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x, Alembic, httpx, Jinja2 + HTMX, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-vehicle-creation-design.md` (binding)

## Global Constraints

- **VINs never appear in logs.** Log at most first/last 4 chars via the `_redact` helper. Tests assert the full VIN is absent from caplog.
- Fail-soft on every NHTSA path: network/API failure → None → the form's free-text path still works; never a 500 from the new endpoints.
- VIN validated (17 chars, alphanumeric excluding I/O/Q) BEFORE any network call.
- Cache-first: `vin_decode_cache` permanent (all-None row = known-unknown, never re-asked); `nhtsa_model_cache` TTL 30 days, serve stale on API error.
- NHTSA calls are NOT logged to `api_quota_log` (eBay-scoped) and NOT counted against eBay quota; timeout 10s.
- Inline NHTSA calls in request handlers are the sanctioned sub-second exception (CLAUDE.md) — worker changes are out of scope.
- Type hints everywhere; UTC-aware timestamps; user-scoped queries filter by `user_id` (vehicles are user-scoped).
- Tests hermetic: monkeypatch `httpx.get` in `nhtsa_vpic`'s namespace; web tests use the `authed_client`/`_force_basic_auth` patterns from `tests/web/test_search_create_category.py`; test DB `oemparts_test` only.
- Never modify existing Alembic migrations. `git checkout -- alembic/` if ruff format churns them.
- Before each commit: `ruff check . && ruff format . && mypy app/ && pytest` (5 pre-existing mypy errors in queries.py/cli.py are known debt; suite currently 183 green and must stay green).
- Commits: imperative present tense, ending with the two standard trailer lines used on this repo's recent commits.

---

### Task 1: nhtsa_model_cache migration + model

**Files:**
- Modify: `app/db/models.py` (add class after `VinDecodeCache`, ~line 372)
- Create: `alembic/versions/<autogen>_add_nhtsa_model_cache.py`
- Test: `tests/db/test_nhtsa_model_cache.py`

**Interfaces:**
- Produces: `NhtsaModelCache(make: str PK, year: int PK, models_json: str, cached_at: datetime)` — consumed by Task 2.

- [ ] **Step 1: Add the model** — after `VinDecodeCache` in `app/db/models.py`:

```python
class NhtsaModelCache(Base):
    """Cached NHTSA model lists per (make, year). TTL enforced in code."""

    __tablename__ = "nhtsa_model_cache"

    make: Mapped[str] = mapped_column(String(100), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    models_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="JSON array of model names"
    )
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
```

- [ ] **Step 2: Generate migration** — `PYTHONPATH=$PWD alembic revision --autogenerate -m "add nhtsa model cache"`; inspect: ONLY creates `nhtsa_model_cache` (composite PK make+year); downgrade drops it; prune stray autogen noise.
- [ ] **Step 3: Apply, roll back, re-apply** — `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` (with PYTHONPATH).
- [ ] **Step 4: Test** — `tests/db/test_nhtsa_model_cache.py` (style of `tests/db/test_listing_aspects.py`): insert a row with `models_json='["LR4", "Range Rover"]'`, read back, assert composite PK enforces one row per (make, year) via merge/overwrite, `cached_at` tz-aware.
- [ ] **Step 5: Suite + commit** — `Add nhtsa_model_cache table`

---

### Task 2: NHTSA vPIC client

**Files:**
- Rewrite: `app/sources/nhtsa_vpic.py` (currently a 5-line stub)
- Test: `tests/sources/test_nhtsa_vpic.py`

**Interfaces:**
- Consumes: `VinDecodeCache`, `NhtsaModelCache` (Task 1), `settings.nhtsa_vpic_base_url`.
- Produces (Task 3 consumes): `DecodedVin(year, make, model, trim, body_class)`; `decode_vin(db: Session, vin: str) -> DecodedVin | None`; `get_models_for_make_year(db: Session, make: str, year: int) -> list[str] | None`; `is_valid_vin(vin: str) -> bool`. Contract: `decode_vin` None = invalid VIN or transient failure; a `DecodedVin` with all-None fields = NHTSA doesn't know it (cached, final). `get_models_for_make_year` None = no data available (caller falls back to free text).

- [ ] **Step 1: Write failing tests** — `tests/sources/test_nhtsa_vpic.py`, monkeypatching `nhtsa_vpic.httpx.get`. Response helper mirrors `tests/sources/test_ebay_item.py`. Cases (real assertions each):

```python
# decode_vin
# 1. invalid VINs (16 chars, 18 chars, contains I/O/Q, empty) -> None, httpx.get NOT called
# 2. clean decode: Results[0] = {"ModelYear": "2012", "Make": "LAND ROVER",
#    "Model": "LR4", "Trim": "HSE", "BodyClass": "SUV...", "ErrorCode": "0"}
#    -> DecodedVin(2012, "LAND ROVER", "LR4", "HSE", ...); cache row written
#    (raw_json non-null, decoded_at stamped)
# 3. ErrorCode "6" but usable Y/M/M -> returned anyway (partial decode)
# 4. cache hit -> httpx.get NOT called, values from cache row
# 5. all-empty decode -> DecodedVin(None×5) AND cached; second call: no API hit
# 6. HTTP 500 / httpx.ConnectError -> None, NO cache row (retry allowed),
#    warning logged, full VIN NOT in caplog (redacted "1FTF…WXYZ" style ok)
# 7. lowercase input with spaces "  5lmjj2h57ae..." normalized to upper/stripped
# get_models_for_make_year
# 8. API success: Results [{"Model_Name": "LR4"}, {"Model_Name": "Range Rover"},
#    {"Model_Name": "LR4"}] -> ["LR4", "Range Rover"] sorted deduped; cached
# 9. cache hit within TTL -> no API call
# 10. cache row older than 30 days -> API re-fetched, cache updated
# 11. stale cache + API error -> stale list returned (serve-stale)
# 12. no cache + API error -> None
```

- [ ] **Step 2: Run to verify failure** — import error.
- [ ] **Step 3: Implement** — `app/sources/nhtsa_vpic.py`:

```python
"""NHTSA vPIC client: VIN decoding and model lists, cached.

Free public API, no key. Fail-soft: any failure returns None and the
vehicle form falls back to free text. VINs are never logged in full —
use _redact() in every log line that mentions one.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import NhtsaModelCache, VinDecodeCache, utcnow

logger = logging.getLogger(__name__)

MODEL_CACHE_TTL_DAYS = 30
_TIMEOUT = 10
# 17 chars, alphanumeric, excluding I/O/Q per the VIN standard.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


@dataclass
class DecodedVin:
    year: int | None
    make: str | None
    model: str | None
    trim: str | None
    body_class: str | None


def is_valid_vin(vin: str) -> bool:
    return bool(_VIN_RE.match(vin.strip().upper()))


def _redact(vin: str) -> str:
    return f"{vin[:4]}…{vin[-4:]}" if len(vin) >= 8 else "…"


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def decode_vin(db: Session, vin: str) -> DecodedVin | None:
    """Decode a VIN via cache-then-NHTSA. None = invalid or transient failure."""
    vin = vin.strip().upper()
    if not _VIN_RE.match(vin):
        return None

    cached = db.get(VinDecodeCache, vin)
    if cached is not None:
        return DecodedVin(
            year=cached.year,
            make=cached.make,
            model=cached.model,
            trim=cached.trim,
            body_class=cached.body_class,
        )

    url = f"{settings.nhtsa_vpic_base_url}/vehicles/DecodeVinValues/{vin}?format=json"
    try:
        response = httpx.get(url, timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("VIN decode failed for %s: %s", _redact(vin), exc)
        return None
    if response.status_code != 200:
        logger.warning(
            "VIN decode HTTP %d for %s", response.status_code, _redact(vin)
        )
        return None
    try:
        results = response.json().get("Results") or []
    except ValueError:
        logger.warning("VIN decode non-JSON body for %s", _redact(vin))
        return None
    row: dict = results[0] if results and isinstance(results[0], dict) else {}

    year_str = _clean(row.get("ModelYear"))
    decoded = DecodedVin(
        year=int(year_str) if year_str and year_str.isdigit() else None,
        make=_clean(row.get("Make")),
        model=_clean(row.get("Model")),
        trim=_clean(row.get("Trim")),
        body_class=_clean(row.get("BodyClass")),
    )
    error_code = _clean(row.get("ErrorCode"))
    if error_code not in (None, "0"):
        logger.info("VIN decode ErrorCode %s for %s", error_code, _redact(vin))

    # Cache even an all-None decode: NHTSA doesn't know this VIN — final.
    db.add(
        VinDecodeCache(
            vin=vin,
            year=decoded.year,
            make=decoded.make,
            model=decoded.model,
            trim=decoded.trim,
            body_class=decoded.body_class,
            raw_json=json.dumps(row),
        )
    )
    db.commit()
    return decoded


def get_models_for_make_year(
    db: Session, make: str, year: int
) -> list[str] | None:
    """Model names for a make+year, cache-then-NHTSA. None = nothing available."""
    key_make = make.strip().lower()
    cached = db.get(NhtsaModelCache, (key_make, year))
    fresh_cutoff = utcnow() - timedelta(days=MODEL_CACHE_TTL_DAYS)
    if cached is not None and cached.cached_at >= fresh_cutoff:
        return json.loads(cached.models_json)

    url = (
        f"{settings.nhtsa_vpic_base_url}/vehicles/GetModelsForMakeYear"
        f"/make/{make.strip()}/modelyear/{year}?format=json"
    )
    try:
        response = httpx.get(url, timeout=_TIMEOUT)
        response.raise_for_status()
        results = response.json().get("Results") or []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("NHTSA models fetch failed for %s %d: %s", make, year, exc)
        if cached is not None:  # serve stale rather than nothing
            return json.loads(cached.models_json)
        return None

    names = sorted(
        {
            name
            for entry in results
            if isinstance(entry, dict)
            and (name := _clean(entry.get("Model_Name"))) is not None
        }
    )
    if cached is None:
        cached = NhtsaModelCache(
            make=key_make, year=year, models_json=json.dumps(names)
        )
        db.add(cached)
    else:
        cached.models_json = json.dumps(names)
        cached.cached_at = utcnow()
    db.commit()
    return names
```

- [ ] **Step 4: Run tests to verify pass.**
- [ ] **Step 5: Lint, mypy, full suite, commit** — `Add NHTSA vPIC client: VIN decode and model lists`

---

### Task 3: Routes, curated makes, and field partials

**Files:**
- Create: `app/web/makes.py`
- Create: `app/web/templates/components/vehicle_fields.html`
- Create: `app/web/templates/components/model_options.html`
- Modify: `app/web/routes/vehicles.py`
- Modify: `app/db/queries.py` (`create_vehicle` gains `vin`)
- Test: `tests/web/test_vehicle_creation.py`

**Interfaces:**
- Consumes: `decode_vin`, `get_models_for_make_year`, `is_valid_vin` (Task 2).
- Produces: `COMMON_MAKES: tuple[str, ...]`; routes `POST /vehicles/decode-vin` (form field `vin_lookup`), `GET /vehicles/models?make=&year=`; `POST /vehicles` accepting `make_text`/`model_text` overrides + optional `vin`; partial `vehicle_fields.html` with context `mode` (`"dropdown"`/`"decoded"`), `decoded`, `error`, `makes`, `years`. Task 4 embeds the same partial in the page.

- [ ] **Step 1: Curated makes** — `app/web/makes.py`:

```python
"""Curated consumer makes for the vehicle form dropdown.

NHTSA's full registry has 10,000+ entries (trailer/coachbuilders) — a
useless dropdown. Values match NHTSA naming; the URL path lookup is
case-insensitive. Free-text fallback covers anything missing here.
"""

COMMON_MAKES: tuple[str, ...] = (
    "Acura", "Alfa Romeo", "Aston Martin", "Audi", "Bentley", "BMW",
    "Buick", "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Ferrari",
    "Fiat", "Fisker", "Ford", "Genesis", "GMC", "Honda", "Hummer",
    "Hyundai", "Infiniti", "Isuzu", "Jaguar", "Jeep", "Kia",
    "Lamborghini", "Land Rover", "Lexus", "Lincoln", "Lotus", "Lucid",
    "Maserati", "Mazda", "McLaren", "Mercedes-Benz", "Mercury", "MINI",
    "Mitsubishi", "Nissan", "Oldsmobile", "Plymouth", "Polestar",
    "Pontiac", "Porsche", "RAM", "Rivian", "Rolls-Royce", "Rover",
    "Saab", "Saturn", "Scion", "Smart", "Subaru", "Suzuki", "Tesla",
    "Toyota", "Volkswagen", "Volvo",
)
```

- [ ] **Step 2: Write failing route tests** — `tests/web/test_vehicle_creation.py`, copying the autouse `_force_basic_auth` (+ `ai_filter_enabled` pin) and `authed_client` fixtures from `tests/web/test_search_create_category.py`. Monkeypatch the NHTSA functions on the ROUTE module's namespace (`vehicles_module.decode_vin` etc.). Cases:
  1. `POST /vehicles/decode-vin` with decode returning `DecodedVin(2012, "LAND ROVER", "LR4", "HSE", "SUV")` → 200, response contains `value="2012"`, `value="LAND ROVER"`, `value="LR4"`, hidden input `name="vin"`.
  2. decode returns None → 200, contains "Could not decode", dropdown mode markup (a `<select`), no 500.
  3. `GET /vehicles/models?make=Land+Rover&year=2012` with models `["LR2", "LR4"]` → options for both plus `value="__other__"`.
  4. models returns None → only the `__other__` option (with "not listed" text).
  5. `POST /vehicles` with `make="Land Rover", model="__other__", model_text="LR4"` → vehicle created with model "LR4".
  6. `POST /vehicles` with `make_text="Koenigsegg", model_text="CC850"` (selects empty) → created with those.
  7. `POST /vehicles` with `vin="SALAG2D40CA000000"` (any valid-format) → stored on the row; with `vin="short"` → vehicle created, vin NULL.
  8. `POST /vehicles` with no resolvable model (`model=""`, no text) → no vehicle row created; response carries `HX-Retarget: #vehicle-fields` header and an error message.
  9. Regression: plain full-form POST (year/make/model as before, via text fields) still creates + returns the HTMX row partial.
- [ ] **Step 3: Run to verify failure.**
- [ ] **Step 4: Implement.**

`app/db/queries.py::create_vehicle` — add parameter `vin: str | None = None` (last, keyword) and pass `vin=vin` into the `Vehicle(...)` constructor.

`app/web/routes/vehicles.py` — add imports:

```python
from app.sources.nhtsa_vpic import decode_vin, get_models_for_make_year, is_valid_vin
from app.web.makes import COMMON_MAKES

YEARS = tuple(range(2027, 1980, -1))
```

New handlers:

```python
@router.post("/decode-vin", response_class=HTMLResponse)
def decode_vin_endpoint(
    request: Request,
    vin_lookup: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """HTMX: decode a VIN and return the fields partial (never 500s)."""
    decoded = decode_vin(db, vin_lookup)
    if decoded is None or not (decoded.year or decoded.make or decoded.model):
        return templates.TemplateResponse(
            request,
            "components/vehicle_fields.html",
            {
                "mode": "dropdown",
                "makes": COMMON_MAKES,
                "years": YEARS,
                "error": (
                    "Could not decode that VIN — pick or type the details instead."
                ),
            },
        )
    return templates.TemplateResponse(
        request,
        "components/vehicle_fields.html",
        {
            "mode": "decoded",
            "decoded": decoded,
            "vin": vin_lookup.strip().upper(),
        },
    )


@router.get("/models", response_class=HTMLResponse)
def model_options(
    request: Request,
    make: str = "",
    year: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """HTMX: <option> list for a make+year (fail-soft to the Other option)."""
    models = None
    if make and make != "__other__" and year:
        models = get_models_for_make_year(db, make, year)
    return templates.TemplateResponse(
        request,
        "components/model_options.html",
        {"models": models or []},
    )
```

`create_vehicle` handler — replace the signature/body head with:

```python
@router.post("/", response_model=None)
def create_vehicle(
    request: Request,
    year: int = Form(...),
    make: str = Form(""),
    model: str = Form(""),
    make_text: str = Form(""),
    model_text: str = Form(""),
    vin: str | None = Form(None),
    trim: str | None = Form(None),
    nickname: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a vehicle. Free-text overrides the dropdowns; VIN optional."""
    final_make = make_text.strip() or (make.strip() if make != "__other__" else "")
    final_model = model_text.strip() or (
        model.strip() if model != "__other__" else ""
    )
    if not final_make or not final_model:
        response = templates.TemplateResponse(
            request,
            "components/vehicle_fields.html",
            {
                "mode": "dropdown",
                "makes": COMMON_MAKES,
                "years": YEARS,
                "error": "Make and model are required — pick or type them.",
            },
        )
        response.headers["HX-Retarget"] = "#vehicle-fields"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    clean_vin = vin.strip().upper() if vin else None
    vehicle = queries.create_vehicle(
        db,
        user_id=current_user.id,
        year=year,
        make=final_make,
        model=final_model,
        trim=trim.strip() if trim else None,
        nickname=nickname.strip() if nickname else None,
        vin=clean_vin if clean_vin and is_valid_vin(clean_vin) else None,
    )
    db.commit()
```

(the HTMX-row/redirect tail of the handler is unchanged).

`components/model_options.html`:

```html
<option value="">— select model —</option>
{% for m in models %}
<option value="{{ m }}">{{ m }}</option>
{% endfor %}
<option value="__other__">Other / not listed — type it below</option>
```

`components/vehicle_fields.html`:

```html
{% if error %}<div class="form-error">{{ error }}</div>{% endif %}
{% if mode == "decoded" %}
<input type="hidden" name="vin" value="{{ vin }}">
<div class="form-group">
    <label for="year">Year</label>
    <input type="number" id="year" name="year" class="form-control" required
           value="{{ decoded.year or '' }}" style="width: 90px;">
</div>
<div class="form-group">
    <label for="make_text">Make</label>
    <input type="text" id="make_text" name="make_text" class="form-control" required
           value="{{ decoded.make or '' }}" style="width: 150px;">
</div>
<div class="form-group">
    <label for="model_text">Model</label>
    <input type="text" id="model_text" name="model_text" class="form-control" required
           value="{{ decoded.model or '' }}" style="width: 140px;">
</div>
<div class="form-group">
    <label for="trim">Trim</label>
    <input type="text" id="trim" name="trim" class="form-control"
           value="{{ decoded.trim or '' }}" style="width: 120px;">
</div>
{% else %}
<div class="form-group">
    <label for="year">Year</label>
    <select id="year" name="year" class="form-control" required style="width: 90px;"
            hx-get="/vehicles/models" hx-include="[name='year'],[name='make']"
            hx-target="#model-select" hx-swap="innerHTML">
        <option value="">Year</option>
        {% for y in years %}<option value="{{ y }}">{{ y }}</option>{% endfor %}
    </select>
</div>
<div class="form-group">
    <label for="make">Make</label>
    <select id="make" name="make" class="form-control" style="width: 160px;"
            hx-get="/vehicles/models" hx-include="[name='year'],[name='make']"
            hx-target="#model-select" hx-swap="innerHTML">
        <option value="">— select make —</option>
        {% for m in makes %}<option value="{{ m }}">{{ m }}</option>{% endfor %}
        <option value="__other__">Other / not listed</option>
    </select>
</div>
<div class="form-group">
    <label for="model-select">Model</label>
    <select id="model-select" name="model" class="form-control" style="width: 160px;">
        <option value="">pick year &amp; make first</option>
    </select>
</div>
<div class="form-group">
    <label for="make_text">Make (type)</label>
    <input type="text" id="make_text" name="make_text" class="form-control"
           placeholder="If not listed" style="width: 130px;">
</div>
<div class="form-group">
    <label for="model_text">Model (type)</label>
    <input type="text" id="model_text" name="model_text" class="form-control"
           placeholder="If not listed" style="width: 130px;">
</div>
<div class="form-group">
    <label for="trim">Trim</label>
    <input type="text" id="trim" name="trim" class="form-control"
           placeholder="Optional" style="width: 100px;">
</div>
{% endif %}
```

- [ ] **Step 5: Run tests to verify pass; lint; mypy; full suite; commit** — `Add VIN decode and model-list endpoints for vehicle creation`

---

### Task 4: Page form rework

**Files:**
- Modify: `app/web/templates/pages/vehicles.html`
- Modify: `app/web/routes/vehicles.py::vehicles_page` (pass `makes`/`years` + mode into the template context)
- Test: extend `tests/web/test_vehicle_creation.py`

**Interfaces:**
- Consumes: `vehicle_fields.html` partial, `COMMON_MAKES`, `YEARS` (Task 3).

- [ ] **Step 1: Write failing tests** — GET `/vehicles` page: contains the VIN input (`name="vin_lookup"`) + Decode button posting to `/vehicles/decode-vin`; contains the Year and Make `<select>`s (assert `Land Rover` option present) and the `#vehicle-fields` container; nickname input still present.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — `vehicles_page` context adds `"makes": COMMON_MAKES, "years": YEARS, "mode": "dropdown"`. Replace the form block in `pages/vehicles.html` (lines 8-34) with:

```html
<!-- Add Vehicle Form -->
<form class="form-inline" style="margin-bottom: 0.5rem;"
      hx-post="/vehicles/decode-vin" hx-target="#vehicle-fields" hx-swap="innerHTML">
    <div class="form-group">
        <label for="vin_lookup">Have a VIN?</label>
        <input type="text" id="vin_lookup" name="vin_lookup" class="form-control"
               maxlength="17" placeholder="17-character VIN" style="width: 220px;">
    </div>
    <div class="form-group">
        <label>&nbsp;</label>
        <button type="submit" class="btn btn-secondary">Decode VIN</button>
    </div>
</form>

<form class="form-inline" hx-post="/vehicles" hx-target="#vehicle-list" hx-swap="beforeend">
    <div id="vehicle-fields" class="form-inline" style="display: contents;">
        {% include "components/vehicle_fields.html" %}
    </div>
    <div class="form-group">
        <label for="nickname">Nickname</label>
        <input type="text" id="nickname" name="nickname" class="form-control"
               placeholder="Optional" style="width: 140px;">
    </div>
    <div class="form-group">
        <label>&nbsp;</label>
        <button type="submit" class="btn btn-primary">Add Vehicle</button>
    </div>
</form>
```

(The decode form is separate so the Decode button doesn't submit the create form; its response swaps into `#vehicle-fields` inside the create form. `display: contents` keeps the inline-form layout flowing.)

- [ ] **Step 4: Run tests; full suite; lint; commit** — `Rework vehicle form: VIN row and cascading dropdowns`

---

### Task 5: Docs

**Files:**
- Modify: `docs/SYSTEM_CONTEXT.md` (changelog row + §5 if it describes vehicle creation)

**Interfaces — EXACT names, do not invent others:** module `app/sources/nhtsa_vpic.py`; functions `decode_vin`, `get_models_for_make_year`, `is_valid_vin`; tables `vin_decode_cache` (existing, now used), `nhtsa_model_cache` (new); constant `COMMON_MAKES` in `app/web/makes.py`; routes `POST /vehicles/decode-vin`, `GET /vehicles/models`; TTL 30 days; NO new settings/env vars; NHTSA calls not in `api_quota_log`. Grep each name against the repo before and after writing.

- [ ] **Step 1: Changelog row** (2026-08-26): VIN decode + Y/M/M dropdowns, fail-soft, caches, free-text fallback, VINs redacted in logs.
- [ ] **Step 2: Verify every identifier via grep; run suite + ruff; commit** — `Update SYSTEM_CONTEXT: vehicle creation overhaul`

---

### Task 6: Live validation with owner (manual — not for subagents)

1. Merge-gate: final review clean, suite green, dashboard restarted.
2. Owner pastes their real LR4 VIN → expect 2012 / Land Rover-ish make / LR4 to fill; create; confirm `vin` stored and VIN nowhere in `logs/app.log` (grep the log for the VIN — must be absent).
3. Dropdown path: 2012 + Land Rover → model list contains LR4; "Other" path reveals nothing broken; free-text-only creation works.
4. Fitment regression: create a search against the new vehicle → category + compatibility_filter resolve.
5. PR after owner sign-off.
