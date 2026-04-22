# CLAUDE.md — OEMPartsAgent

> This file is read by Claude Code at the start of every session. Keep it short, operational, and current.
> For full system context, see [`docs/SYSTEM_CONTEXT.md`](docs/SYSTEM_CONTEXT.md).

---

## Project at a Glance

**What:** A personal-then-commercial parts-tracking dashboard backed by the eBay Browse API. Users define vehicles and parts they want to track; scheduled fetchers collect matching eBay listings; the dashboard surfaces them with price history. Revenue via eBay Partner Network (EPN) affiliate links.

**Owner:** Hobbyist Python developer. Prefers detailed instructions and explanations. Not a professional developer — favor clarity over cleverness.

**Companion project:** StockAgent. Same operational pattern (Docker Compose on EC2, FastAPI + HTMX, PostgreSQL, Caddy). Lessons from StockAgent transfer directly.

---

## Current Phase

**Phase 0** — Specification complete. Ready to begin Phase 1 (single-user MVP).

Phase 1 goal: Owner can track ~20 parts for a 2012 Land Rover LR4 on a local Docker Compose stack, with fetch cycles running via cron and a working FastAPI + HTMX dashboard.

**Do not skip ahead to Phase 2+ features** (Clerk auth, EC2 deployment, email alerts, EPN integration) without explicit instruction. Phase discipline is deliberate — see `SYSTEM_CONTEXT.md` Section 10.

---

## Tech Stack (quick reference)

- **Language:** Python 3.11
- **Web framework:** FastAPI
- **Templates:** Jinja2 + HTMX (no React, no build step)
- **Database:** PostgreSQL 15 (via Docker)
- **ORM:** SQLAlchemy 2.x (async is fine but prefer sync for Phase 1 simplicity)
- **Migrations:** Alembic
- **Config:** Pydantic Settings (loads `.env`)
- **Scheduling:** cron (inside worker container)
- **HTTP client:** `httpx` (sync) for eBay API calls
- **Testing:** `pytest` + `pytest-asyncio`
- **Lint/format:** `ruff` (both linter and formatter)
- **Type checking:** `mypy` (strict mode on new modules)

---

## Coding Conventions

- **Type hints are required** on all function signatures. `mypy` should pass on new code.
- **Money uses `decimal.Decimal`**, never `float`. DB column is `Numeric(10, 2)`.
- **Timestamps are UTC-aware** (`datetime.now(timezone.utc)`). DB columns are `TIMESTAMPTZ`.
- **UUIDs for primary keys** everywhere (not auto-increment integers).
- **No secrets in code or tests.** All secrets via `.env` / Pydantic Settings.
- **User-scoped queries must include `user_id` filter.** This is the multi-tenancy contract. If you write a query against a user-scoped table without `.filter(Model.user_id == current_user.id)`, that's a bug — flag it.
- **No `print()` for logging.** Use the `logging` module. `print()` is fine only in CLI user-facing output.
- **Prefer explicit over clever.** The owner reads this code to learn. Verbose and clear beats terse and opaque.

---

## File Layout

Full map in `SYSTEM_CONTEXT.md` Section 3. Key directories:

```
app/
├── config.py            # Pydantic Settings
├── worker/              # CLI + scheduled jobs
├── sources/             # External API clients (ebay_*, nhtsa_*)
├── core/                # Business logic (search_runner, deduplicator, affiliate)
├── web/                 # FastAPI app, routes, templates, static
├── auth/                # Pluggable auth (basic for Phase 1, clerk for Phase 2)
└── db/                  # Models, session, queries

docs/                    # SYSTEM_CONTEXT.md and related
alembic/                 # Migrations
scripts/                 # Utility scripts (seed data, etc.)
tests/                   # Mirrors app/ structure
```

---

## Common Commands

```bash
# Activate venv
source .venv/bin/activate

# Start local DB
docker compose -f docker-compose.local.yml up -d db

# Run migrations
PYTHONPATH=$PWD alembic upgrade head

# Create a new migration
PYTHONPATH=$PWD alembic revision --autogenerate -m "describe change"

# Run the dashboard
PYTHONPATH=$PWD uvicorn app.web.main:app --reload --port 8000

# Run the CLI
./oemparts fetch --cycle=manual --search-id=1

# Run tests
pytest

# Lint + format
ruff check .
ruff format .

# Type check
mypy app/
```

---

## Database Workflow

- **Never modify existing migration files.** Always create a new migration for schema changes.
- **Run `alembic upgrade head` after pulling new migrations.**
- **Test migrations locally before committing.** Generate the migration, inspect the SQL, apply it, roll back, apply again.
- **Foreign keys use `ON DELETE CASCADE`** for user-scoped data (when a user is deleted, their vehicles/searches/alerts go too).
- **Seed data is in `scripts/seed_dev_user.py`** — run this after migrations to get a working local environment.

---

## What Not to Do

- **Don't leak user data across tenants.** Every user-scoped query must filter by `user_id`. The one acceptable shared-data case is `listings` (eBay listings are public); the user-scoped reference lives in `search_listings`.
- **Don't hit eBay's Browse API without the OAuth token cached.** The `ebay_oauth.py` module handles token lifecycle. Don't bypass it.
- **Don't call eBay APIs in request handlers.** All external API calls happen in the worker (via cron). Request handlers read from the database only. (Exception: VIN decode and Taxonomy lookups during vehicle creation, which need sub-second response — these are acceptable inline with proper caching.)
- **Don't skip Alembic and modify the DB directly** — even for quick testing. If you need a schema change, create a migration.
- **Don't add new dependencies without justification.** Check `requirements.txt` first; if something similar exists, use it.
- **Don't build Phase 2+ features speculatively.** Clerk auth, EC2 deployment, email alerts, EPN reporting are all deferred until their phase. Building them early creates dead code.
- **Don't log secrets, VINs, or full access tokens.** Truncate to first/last 4 chars if you must log for debugging.
- **Don't silently swallow exceptions.** Log at `warning` or `error` level with context. A specific exception type is better than a bare `except`.

---

## eBay API Specifics

- **OAuth:** Client-credentials grant. Token lasts ~2 hours. Cache in-process; fall back to DB (`oauth_tokens` table) on cold start.
- **Browse API endpoint:** `https://api.ebay.com/buy/browse/v1/item_summary/search`
- **Required headers:** `Authorization: Bearer {token}`, `X-EBAY-C-MARKETPLACE-ID: EBAY_US`
- **Affiliate header (Phase 3+):** `X-EBAY-C-ENDUSERCTX: affiliateCampaignId={EPN_CAMPAIGN_ID}`. When set, responses include `itemAffiliateWebUrl`.
- **Compatibility filter:** Only works within eBay Motors categories (6028 + descendants). Silently ignored elsewhere.
- **Rate limit:** 5,000 calls/day default at the app (not user) level. Log every call to `api_quota_log` for observability.
- **Pagination:** Use `offset` + `limit`. Max `limit=200` per call. Cap at `FETCH_MAX_LISTINGS_PER_QUERY` (default 50) — deeper pages are usually noise.

---

## Git Workflow

- **Branch naming:** `phase1/feature-name`, `fix/bug-description`, `docs/what-you-updated`
- **Commits:** Imperative present tense ("Add VIN decoder", not "Added VIN decoder")
- **Phase tags:** Tag releases as `v0.1.0-phase1`, `v0.2.0-phase2`, etc.
- **Changelog:** Update the `SYSTEM_CONTEXT.md` Changelog section for any user-visible or architectural change.
- **Never commit `.env`.** Never commit eBay keys, Clerk keys, or EPN campaign ID. `.env.example` gets committed with placeholder values.

---

## When in Doubt

- **Read `SYSTEM_CONTEXT.md` first** — it's the source of truth for architecture and phasing decisions.
- **Ask the owner** if a request seems to violate phase discipline or multi-tenancy rules. Better to confirm than to build the wrong thing.
- **Mirror StockAgent patterns** when an operational question comes up (deployment, cron, secrets, logging). The owner has already made these decisions there.
- **Prefer boring tech.** Python stdlib before third-party, synchronous before async, server-rendered HTML before SPA.
