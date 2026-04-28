# OEMPartsAgent — System Context (Living Document)

> **Audience:** Developers, AI assistants, and outside collaborators.
> **How to use:** Read top-to-bottom for full context, or jump to a section. If you're an AI assistant starting a new conversation, this is your single-source briefing.
> **Keeping it current:** Update the [Changelog](#changelog) at the bottom whenever significant changes ship. Stale docs are worse than no docs.
> **Status:** Phase 1 MVP complete (2026-04-26). All core components implemented and validated end-to-end with real eBay data. See [Changelog](#changelog) for details.

---

## 1. Why This Exists

**The problem:** Enthusiast car owners hunting for OEM parts on eBay face three persistent frustrations. First, eBay's native search returns noise — parts that mention a vehicle in the title but don't actually fit, warrants of listings that expired, and no way to track price drift over time. Second, there's no easy way to run a watchlist of multiple parts simultaneously (coolant pipe, water pump, timing chain, injectors, etc.) and see a consolidated view. Third, researching a part often happens over days or weeks, but eBay's 24-hour cookie and ephemeral listings mean good deals are missed and price context is lost.

**What OEMPartsAgent does:** Automates a personal parts-hunting dashboard backed by the eBay Browse API. Users define a vehicle (by Year/Make/Model or by VIN) and a list of parts they're tracking. The system fetches matching listings on a schedule, stores them in a database, enriches each listing with fitment compatibility data, tracks price history over time, and surfaces the results in a clean dashboard. Each outbound click to eBay is wrapped in an eBay Partner Network (EPN) affiliate URL so qualifying purchases generate commission revenue.

**Target user:** Initially the project owner (tracking parts for a 2012 Land Rover LR4). Designed for expansion to enthusiast communities where vehicles have known failure-prone parts and active owner engagement — Land Rover, classic Mustang, Miata, air-cooled VW, etc.

**What it is NOT:** Not a parts marketplace — we never hold inventory or handle transactions. Not a price prediction tool — we report observed prices, not forecasted ones. Not a universal parts catalog — we're a search-and-track layer over eBay's inventory, not a replacement for RockAuto or the dealer parts database. Not a bidding bot — we link users to listings, we don't place bids.

**Design philosophy:** Multi-tenant from day one, single-tenant in practice. Every table with user-scoped data has a `user_id` foreign key from the first migration. The database, auth flow, and API structure are built as if 10,000 users exist, even while only one does. This avoids the rewrite trap that kills most "hobby project that went commercial" attempts.

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    EC2 (t3.small, Ubuntu)                     │
│                                                               │
│  ┌───────────┐  ┌───────────┐  ┌──────────┐  ┌────────┐     │
│  │PostgreSQL │  │  Worker   │  │Dashboard │  │ Caddy  │     │
│  │  (data)   │←─│(fetcher + │  │(FastAPI  │←─│(reverse│←── HTTPS
│  │  port 5432│  │   cron)   │  │ + HTMX   │  │ proxy) │     │
│  └───────────┘  └───────────┘  │ port 8000│  │ 80/443 │     │
│                                 └──────────┘  └────────┘     │
└──────────────────────────────────────────────────────────────┘
         │                │                │
         │                ▼                ▼
         │        ┌──────────────┐  ┌──────────────┐
         │        │  eBay APIs   │  │  NHTSA vPIC  │
         │        │  (OAuth,     │  │  (free VIN   │
         │        │   Browse,    │  │   decoder)   │
         │        │   Taxonomy)  │  └──────────────┘
         │        └──────────────┘
         │
         ▼
  ┌──────────────┐
  │  Clerk/Auth0 │  (Phase 2+)
  │   external   │
  │     auth     │
  └──────────────┘
```

**Stack:** Python 3.11 | PostgreSQL 15 | SQLAlchemy + Alembic | FastAPI + HTMX + Jinja2 | Docker Compose | Caddy (auto-SSL) | AWS EC2

**Four Docker services** defined in `docker-compose.aws.yml`:
- **db** — PostgreSQL 15, persistent volume
- **worker** — Runs CLI commands via cron (fetch cycles, taxonomy sync, cleanup)
- **dashboard** — FastAPI app on port 8000 with HTMX partial rendering
- **caddy** — Reverse proxy with automatic Let's Encrypt SSL for oempartsagent.com

**Rationale for this stack:** Mirrors the StockAgent deployment pattern intentionally — same Docker Compose structure, same Caddy reverse proxy, same EC2 sizing. This gives operational consistency (one mental model for deploys, secrets, cron, logs) and lets lessons learned on StockAgent transfer directly. FastAPI + HTMX + Jinja2 avoids the complexity of a separate frontend build pipeline while still producing a polished, interactive UI.

---

## 3. Codebase Map

```
OEMPartsAgent/
├── app/
│   ├── config.py                    # Pydantic Settings (loads .env)
│   ├── worker/
│   │   ├── cli.py                   # ALL CLI commands
│   │   ├── fetcher.py               # Fetch cycle orchestrator
│   │   └── cleanup.py               # Expired listing purge
│   ├── sources/                     # External data provider clients
│   │   ├── ebay_oauth.py            #   OAuth token management (2hr cache)
│   │   ├── ebay_browse.py           #   Browse API search + item details
│   │   ├── ebay_taxonomy.py         #   Taxonomy API (Y/M/M cascading)
│   │   └── nhtsa_vpic.py            #   VIN decoder (free, no auth)
│   ├── core/                        # Business logic
│   │   ├── search_runner.py         #   Execute searches, persist listings
│   │   ├── deduplicator.py          #   Cross-user query sharing
│   │   ├── affiliate.py             #   EPN URL construction
│   │   ├── compatibility.py         #   Fitment filter builder
│   │   ├── price_tracker.py         #   Price history + delta detection
│   │   └── vin_resolver.py          #   VIN → vehicle record
│   ├── web/                         # FastAPI + HTMX dashboard
│   │   ├── main.py                  #   FastAPI app, Jinja2, static, startup
│   │   ├── routes/                  #   Route handlers (one per page)
│   │   │   ├── home.py              #     Landing / overview
│   │   │   ├── vehicles.py          #     My vehicles (add/edit/delete)
│   │   │   ├── searches.py          #     Parts watchlist management
│   │   │   ├── listings.py          #     Listing browser + filters
│   │   │   ├── price_history.py     #     Price charts per part
│   │   │   ├── alerts.py            #     Configured alerts (Phase 3)
│   │   │   ├── account.py           #     User settings / subscription
│   │   │   ├── system_status.py     #     Health dashboard
│   │   │   ├── api.py               #     HTMX partials + JSON endpoints
│   │   │   └── _helpers.py          #     Shared: affiliate URL, formatting
│   │   ├── templates/
│   │   │   ├── base.html            #   Sidebar nav, header, main content
│   │   │   ├── components/          #   Reusable partials (listing card, etc.)
│   │   │   └── pages/               #   One template per page
│   │   └── static/
│   │       ├── css/theme.css        #   Dark theme + component styles
│   │       └── js/app.js            #   HTMX config, chart helpers
│   ├── auth/
│   │   ├── dependencies.py          #   FastAPI auth dependency injection
│   │   ├── basic.py                 #   Phase 1: single-user basic auth
│   │   └── clerk.py                 #   Phase 2+: Clerk integration
│   └── db/
│       ├── models.py                # SQLAlchemy models (all tables)
│       ├── session.py               # DB session factory
│       └── queries.py               # Query helpers
├── alembic/                         # Database migrations
├── docs/
│   ├── SYSTEM_CONTEXT.md            # ← You are here
│   ├── architecture/                # System design deep-dives
│   ├── setup/                       # Local dev setup
│   ├── deployment/                  # EC2 deployment guide
│   └── legal/                       # eBay TOS notes, EPN compliance
├── scripts/                         # Utility scripts
├── docker-compose.aws.yml           # Production Docker Compose
├── Caddyfile                        # Reverse proxy config
├── oemparts                         # CLI entry point
├── requirements.txt
└── CLAUDE.md                        # Project context for Claude Code
```

**Web architecture:** FastAPI routes import directly from the data layer (`app/db/`, `app/core/`). HTMX handles partial page updates (e.g., adding a new search without reloading the dashboard). Jinja2 renders server-side; no React, no build step.

**Auth architecture:** Designed with a pluggable auth dependency. Phase 1 uses `basic.py` (hardcoded single user). Phase 2 swaps to `clerk.py` (Clerk-hosted auth). Route handlers depend on `get_current_user()` and remain unchanged across the swap.

---

## 4. Data Providers

| Provider | What It Provides | Rate Limits | Notes |
|----------|-----------------|-------------|-------|
| **eBay Browse API** | Item search, item details, compatibility check | 5,000 calls/day (default app-level) | Primary data source. Use `compatibility_filter` for fitment accuracy. Returns `itemAffiliateWebUrl` when EPN campaign ID is passed via header. |
| **eBay OAuth** | Client credentials access tokens | N/A | Token expires ~2 hours. Cache aggressively (in-process + DB fallback). Single token shared across all users. |
| **eBay Taxonomy API** | Valid Year/Make/Model hierarchy per marketplace | Same pool as Browse | Used to populate cascading dropdowns. Cache full taxonomy locally and refresh weekly. |
| **NHTSA vPIC API** | VIN → Year/Make/Model/Trim decoding | Free, no key, "reasonable use" | US government API. Endpoint: `https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{VIN}?format=json`. Limited international coverage — VIN lookup is US-centric. |
| **eBay Partner Network** | Affiliate tracking + commission | N/A (reporting API separate) | Not a data provider strictly; campaign ID is injected into Browse API requests via `X-EBAY-C-ENDUSERCTX` header. |

**Rate limit headroom (Phase 1, single user):** 20 tracked parts × 4 fetch cycles/day ≈ 80 calls/day. Well under 5,000/day ceiling.

**Rate limit at scale (projected Phase 3, 100 users):** 100 users × 20 parts × 4 cycles/day = 8,000 calls/day *without* deduplication. With deduplication (shared listings across users searching the same query), this drops to an estimated 2,000-3,000/day. Still safe, but monitoring required.

**Production API approval:** eBay's Browse API is usable in production under the default keyset, but affiliate-bearing production use technically requires review when scaling beyond personal use. See [Section 14: Legal & Compliance](#14-legal--compliance).

---

## 5. Core Engine: Parts Search & Tracking

### How It Works

1. **User defines vehicle(s)** via the dashboard — either by selecting Year/Make/Model from cascading dropdowns (sourced from eBay Taxonomy API) or by entering a VIN (decoded via NHTSA vPIC).
2. **User defines searches** — each search is a (vehicle, part query) tuple, e.g., "2012 Land Rover LR4" + "coolant crossover pipe". Optional: OEM number, max price.
3. **Scheduled worker executes fetch cycles** — iterates over active searches, builds Browse API queries with `compatibility_filter` for fitment, fetches results, normalizes them.
4. **Deduplicator checks** — before writing, the system checks if the same query has already been fetched recently (within configured TTL) by another user. If so, results are reused rather than re-fetched.
5. **Persistence** — listings stored in global `listings` table (one row per `ebay_item_id`), joined to searches via `search_listings`. Prices snapshotted to `price_history` on every fetch.
6. **Affiliate URL generation** — when rendering listings in the dashboard, the stored `ebay_item_url` is combined with the EPN campaign ID at display time (or pre-computed and stored as `affiliate_url`).
7. **Dashboard surfaces results** — filterable by vehicle, part, price range, condition, seller rating. Price history charts on click.

### Key Algorithmic Pieces

**Part query construction:** A single Browse API query is built from the descriptive text plus the vehicle's year/make/model. When a search has an OEM number AND `oem_only=true`, results are post-filtered: a listing is kept only if its title contains the word "OEM", the word "Genuine", or the OEM part number itself (with hyphens/spaces stripped). The Browse API has no native title-must-contain filter, so this trimming is done in the worker after the response is returned. The toggle defaults to ON for newly created searches that include an OEM number, and OFF otherwise; users can flip it per search from the dashboard.

**Compatibility filter:** For categories that support fitment (eBay Motors categories 6028 and descendants), the API call includes `compatibility_filter=Year:2012;Make:Land Rover;Model:LR4`. This eliminates the bulk of false positives where a title happens to contain "LR4" but the part doesn't actually fit.

**Listing TTL:** Listings have an `ebay_end_date` from the API. Expired listings are marked `is_active=false` but retained for historical price context. A nightly `cleanup` job purges listings older than 180 days with no recent price updates.

**Deduplication logic:** The `deduplicator` module keys on a normalized query string plus vehicle compatibility. If two users both track "LR4 water pump" for 2012 Land Rover LR4, one API call serves both. Their respective `search_listings` rows reference the same underlying `listings` rows.

### Fetch Schedule (EC2 cron)

| Time (UTC) | Command | Purpose |
|-----------|---------|---------|
| 02:00 | `oemparts fetch --cycle=nightly` | Full refresh of all active searches |
| 08:00, 14:00, 20:00 | `oemparts fetch --cycle=intraday` | Refresh high-priority searches only (user-flagged) |
| 03:00 | `oemparts cleanup` | Purge expired listings, archive old price history |
| Sunday 04:00 | `oemparts taxonomy-sync` | Refresh Y/M/M data from eBay Taxonomy API |

**Why once-daily baseline:** Auto parts inventory churns much more slowly than stocks. Four refreshes per day is enough to catch meaningful new listings and price drops without burning API quota.

---

## 6. Multi-Tenancy Design

This project is architected as a multi-tenant system from day one, even though Phase 1 operates with a single user. This section documents the tenancy model explicitly because it affects every query, every feature, and every future decision.

**Tenancy unit:** A `user` is the tenancy boundary. Users do not share vehicles, searches, or alerts with each other. A user can own multiple vehicles, and each vehicle can have multiple searches.

**Shared vs. isolated data:**
- **Fully isolated per user:** `users`, `vehicles`, `searches`, `alerts`, `user_preferences`, `subscriptions` (future)
- **Shared globally (cached across users):** `listings`, `price_history`, `taxonomy_cache`, `vin_decode_cache`
- **Join tables (user-scoped references to shared data):** `search_listings`

**Why shared listings are safe:** eBay listings are public. If two users both search "LR4 water pump," the listings returned are identical — there's no reason to store them twice or fetch them twice. The `search_listings` join table carries the user-scoped relationship.

**Authorization enforcement:** Every route handler that queries user-scoped tables injects `user_id` filter via a FastAPI dependency (`get_current_user()`). There is no route that returns data without a user context, with the single exception of public marketing pages. All ORM queries must include `.filter(Model.user_id == current_user.id)` — this is checked in code review.

**Rate limit attribution:** Browse API calls are made under a single application-level keyset, but the system logs calls per `user_id` in `api_quota_log` for future per-user throttling if needed. Phase 1 does not throttle individual users, but the data is there when it matters.

---

## 7. Database Schema (Key Tables)

### Core Tables
- **users** — id (UUID), auth_provider_id (external Clerk/Auth0 ID), email, created_at, subscription_tier (free/pro), status
- **vehicles** — id, user_id (FK), year, make, model, trim, vin (nullable), nickname, created_at
- **searches** — id, user_id (FK), vehicle_id (FK), query_text, oem_number (nullable), max_price (nullable), condition_filter (nullable: New/Used), oem_only (bool, default false), is_active, is_high_priority, created_at, last_fetched_at
- **listings** — id, ebay_item_id (UNIQUE), title, price, currency, condition, seller_name, seller_feedback_score, seller_feedback_pct, item_url, image_url, ebay_end_date, is_active, first_seen_at, last_seen_at, category_id, compatibility_checked (bool)
- **search_listings** — search_id (FK), listing_id (FK), matched_at, UNIQUE(search_id, listing_id)
- **price_history** — id, listing_id (FK), price, recorded_at — separate table to prevent `listings` bloat
- **alerts** — id, user_id (FK), search_id (FK), listing_id (FK), alert_type (price_drop/new_listing), triggered_at, notified_at, channel (email/none)

### Cache Tables (Shared Across Users)
- **taxonomy_cache** — category_id, marketplace, year, make, model, trim (nullable), raw_json, refreshed_at
- **vin_decode_cache** — vin (PK), year, make, model, trim, body_class, raw_json, decoded_at

### Operational Tables
- **fetch_runs** — id, started_at, completed_at, cycle_type (nightly/intraday/manual), searches_processed, listings_fetched, listings_new, listings_updated, api_calls_made, errors, status
- **api_quota_log** — id, user_id (FK, nullable for system calls), provider (ebay_browse/ebay_taxonomy/nhtsa), called_at, status_code
- **oauth_tokens** — id, provider (ebay), access_token (encrypted), expires_at, created_at — DB fallback if in-memory cache is cold

All tables use UUID primary keys (except where noted). All timestamps are `TIMESTAMPTZ` (UTC-aware). Every user-scoped table has `user_id` as a non-null foreign key with `ON DELETE CASCADE`.

---

## 8. Local Development Setup

### Prerequisites
- Python 3.11+
- Docker Desktop (for PostgreSQL)
- eBay Developer account with production keyset (Client ID + Client Secret)
- eBay Partner Network account (Campaign ID) — can be obtained after dev account
- `.env` file with credentials (see [Section 11](#11-configuration-reference))

### Quick Start

```bash
# Clone and setup
git clone <repo-url>
cd OEMPartsAgent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys (see Section 11)

# Start database
docker compose -f docker-compose.local.yml up -d db

# Run migrations
PYTHONPATH=$PWD alembic upgrade head

# Seed a user + vehicle for local testing
PYTHONPATH=$PWD python scripts/seed_dev_user.py

# Test a manual fetch
./oemparts fetch --cycle=manual --search-id=1

# Launch dashboard
PYTHONPATH=$PWD uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --reload
# → http://localhost:8000
```

### Local vs. Production Differences

| Setting | Local | Production (Docker) |
|---------|-------|-------------------|
| `POSTGRES_HOST` | `127.0.0.1` | `db` |
| `POSTGRES_PORT` | `5442` | `5432` |
| `AUTH_BACKEND` | `basic` | `clerk` (Phase 2+) |
| `EBAY_ENV` | `production` or `sandbox` | `production` |
| Python | `.venv/bin/python` | System Python in container |
| Secrets | `.env` file | AWS SSM Parameter Store |

**Note on ports:** Port 5442 chosen for local Postgres to avoid collision with StockAgent's 5440 if both projects run on the same machine.

---

## 9. Working With This Codebase

### Safe to Change
- Dashboard routes and templates (`app/web/routes/`, `app/web/templates/`) — isolated UI components
- Listing normalization rules (`app/core/search_runner.py`) — additive, easy to tune
- OEM-only title filter heuristics (`app/core/oem_filter.py`) — covered by unit tests, isolated, easy to refine without ripple
- Scheduled fetch times (`docker/entrypoint.worker.sh`) — adjust cadence freely
- Configuration defaults (`app/config.py`)

### Change With Care
- **`app/core/deduplicator.py`** — Cross-user listing sharing. A bug here could leak one user's searches into another user's dashboard. Any change requires a multi-user test scenario.
- **`app/core/affiliate.py`** — EPN URL construction. Malformed URLs = no commission. Changes need manual verification against an actual EPN click that converts.
- **`app/sources/ebay_browse.py`** — Browse API client. Rate limit handling and OAuth token refresh are tuned carefully. Changes can burn through API quotas or silently fail all fetches.
- **`app/db/models.py`** — Schema changes require Alembic migrations. Test locally before deploying. Never modify existing migration files; always create a new migration.
- **`app/auth/`** — Auth changes touch every route. Breaking these locks users out or, worse, lets them access each other's data.

### Do Not Change Without Discussion
- The `users` / `vehicles` / `searches` foreign key chain — data integrity foundation
- Multi-tenancy filter enforcement in ORM queries — single source of data isolation
- `listings` table uniqueness on `ebay_item_id` — deduplication depends on this

### Known Gotchas (anticipated; to be verified during Phase 1)

- **OAuth token lifetime:** eBay tokens expire in ~2 hours. The client must check expiry and refresh before each call. In-memory cache is faster but disappears on container restart — DB fallback exists for cold start.
- **Compatibility filter is category-scoped:** Only eBay Motors (category 6028 and descendants) supports `compatibility_filter`. For parts that live in other categories (e.g., "tools" or "electronics"), the filter is silently ignored. Search runner should log a warning in this case.
- **EPN 24-hour cookie is brutal:** A user researching a part Tuesday who buys Friday earns you nothing. UX implication: surface "Buy on eBay" CTAs only when user intent is high (e.g., when they mark a listing as "interested"), not on every card.
- **Affiliate links must not appear on eBay property:** EPN TOS prohibits affiliate links from being posted as messages or listings on eBay itself. Users must click through from an external site (our dashboard). Not a technical issue but a policy constraint to document clearly.
- **Part number matching is fuzzy:** Sellers inconsistently include OEM numbers in titles. Dual-query strategy (OEM + descriptive) is partial mitigation, not a cure. Accept that some listings will be missed.
- **VIN decode coverage is uneven:** NHTSA vPIC decodes US-market VINs reliably but has partial coverage for grey-market imports, older vehicles (pre-1981 VINs aren't standardized), and non-US vehicles. UX should fall back to manual Y/M/M entry gracefully.
- **Taxonomy API returns thousands of categories:** Caching the full tree is necessary. Refreshing weekly is sufficient — car makes/models don't change mid-week.
- **Pydantic Settings loads `.env` once at import:** Container restart required after env changes. Same behavior as StockAgent.
- **eBay listing URLs vs. affiliate URLs:** Store both. The raw `item_url` is useful for debugging and for cases where affiliate attribution shouldn't apply (e.g., internal testing). The `affiliate_url` is what the user clicks.
- **Price is a Decimal, not a float:** Money values use `Numeric(10, 2)` in Postgres and `decimal.Decimal` in Python. Float arithmetic on money leads to rounding bugs.
- **`is_active` on listings is write-once-then-maintain:** When a listing first appears, it's active. When its `ebay_end_date` passes or it disappears from fetch results for N consecutive cycles, mark inactive. Don't delete — historical price context matters.
- **Single eBay app keyset serves all users:** All API calls share one OAuth token. Rate limits are app-level, not user-level. Per-user quota tracking in `api_quota_log` is for internal observability, not for gating.

---

## 10. Phase Roadmap

The project is explicitly phased to defer commercial complexity until the core value is proven. Each phase has exit criteria.

### Phase 0 — Specification (complete)
Write this document. Set up GitHub repo with `CLAUDE.md` context file. Purchase domain oempartsagent.com and park it on Cloudflare. Create eBay Developer account and generate production keyset. Create eBay Partner Network account and obtain Campaign ID. **Exit:** Spec signed off, repo scaffolded, credentials in hand. **Completed 2026-04-22.**

### Phase 1 — Single-User MVP (complete)
Backend: OAuth token management (3-tier caching), Browse API client with condition filtering, search runner (enriches query with vehicle year/make/model), deduplicator with TTL, SQLAlchemy models, Alembic migrations, CLI-driven fetcher with manual/nightly/intraday cycles.
Frontend: Dark-themed FastAPI + HTMX dashboard with pages for Vehicles, Searches, Listings, Price History. HTTP Basic auth with session cookie. "Fetch Now" button per search. Auto-fetch on new search creation. Condition filter (New/Used/Any) per search.
Deployment: Local Docker Compose only. Not yet on EC2.
**Exit:** Owner can track 20+ parts for the LR4 locally, fetch cycles run reliably via CLI, dashboard renders real eBay listings with working filters. **Completed 2026-04-26.**
**Note:** compatibility_filter requires leaf-level eBay category IDs (taxonomy module, Phase 2+). Phase 1 workaround: vehicle year/make/model prepended to search query text.

### Phase 2 — Multi-Tenancy + Deployment (~2 weekends)
Add Clerk auth integration. Migrate `basic.py` auth to `clerk.py`. Activate real multi-user pathways (signup, login, logout). Deploy to EC2 with Docker Compose, Caddy reverse proxy, SSL for oempartsagent.com. Add VIN decoding via NHTSA. Add cascading Y/M/M dropdowns via eBay Taxonomy API. Invite 2-3 friends with different vehicles to stress-test.
**Exit:** 3+ users with different vehicles using the live site for 2 weeks without major incidents.

### Phase 3 — Alerts + EPN Monetization (~2-3 weekends)
Add price-drop alerts via email (SendGrid or AWS SES). Add "new listing" alerts for active searches. Integrate EPN campaign ID into all outbound listing URLs. Apply for eBay production API approval for affiliate use at scale. Add EPN reporting integration to track commissions (read-only dashboard view for project owner). Harden observability: System Status page, health banner (mirrored from StockAgent pattern).
**Exit:** Affiliate links generating at least test-level commission events. System uptime >99% over 30 days. eBay production approval granted.

### Phase 4 — Public Launch (timing TBD, conditional on Phase 3 signal)
Marketing page. Content strategy targeting enthusiast communities (Reddit, model-specific forums). Optional: freemium tier with paid unlocks (unlimited searches, longer price history, priority fetch frequency) via Stripe. Decision point on whether to scope the product as "LR-focused tool" vs. "universal parts tracker" based on user feedback from Phases 2-3.
**Exit:** Self-sustaining (EPN revenue covers hosting) or decision to sunset. No pressure to force this phase if the earlier signal is weak.

---

## 11. Configuration Reference

All settings are in `.env` (local) or AWS SSM Parameter Store (production).

```bash
# ── Core ──
DATABASE_URL=postgresql://oemparts:...@localhost:5442/oemparts
LOG_LEVEL=INFO

# ── eBay API ──
EBAY_ENV=production                          # production | sandbox
EBAY_CLIENT_ID=...                           # from developer.ebay.com
EBAY_CLIENT_SECRET=...
EBAY_MARKETPLACE_ID=EBAY_US                  # EBAY_US | EBAY_GB | etc.
EBAY_OAUTH_SCOPE=https://api.ebay.com/oauth/api_scope

# ── eBay Partner Network ──
EPN_CAMPAIGN_ID=...                          # 10-digit campaign ID
EPN_ENABLED=true                             # toggle for testing without affiliate tracking

# ── External ──
NHTSA_VPIC_BASE_URL=https://vpic.nhtsa.dot.gov/api

# ── Auth ──
AUTH_BACKEND=basic                           # basic (Phase 1) | clerk (Phase 2+)
BASIC_AUTH_USERNAME=...                      # Phase 1 only
BASIC_AUTH_PASSWORD=...                      # Phase 1 only
CLERK_SECRET_KEY=...                         # Phase 2+
CLERK_PUBLISHABLE_KEY=...                    # Phase 2+
SESSION_SECRET_KEY=<32-byte-hex>             # FastAPI session middleware

# ── Fetching ──
FETCH_DEFAULT_TTL_MINUTES=240                # Dedup cache TTL (4 hours)
FETCH_MAX_LISTINGS_PER_QUERY=50              # Browse API per-query limit
FETCH_API_RATE_LIMIT_PER_MIN=30              # Internal throttle below eBay's limits

# ── Alerts (Phase 3) ──
ALERTS_ENABLED=false                         # Phase 3 toggle
AWS_SES_REGION=us-east-1
AWS_SES_FROM_EMAIL=alerts@oempartsagent.com

# ── Cleanup ──
LISTING_INACTIVE_AFTER_MISSING_CYCLES=3      # Mark inactive after N missed fetches
LISTING_ARCHIVE_AFTER_DAYS=180               # Archive (not delete) after N days
```

---

## 12. CLI Quick Reference

```bash
# ── Fetching ──
./oemparts fetch --cycle=nightly                        # Refresh all active searches
./oemparts fetch --cycle=intraday                       # Refresh high-priority only
./oemparts fetch --cycle=manual --search-id=N           # Refresh single search (testing)
./oemparts fetch --cycle=manual --user-id=U             # Refresh all searches for a user

# ── Taxonomy & Reference Data ──
./oemparts taxonomy-sync                                # Pull latest Y/M/M from eBay
./oemparts decode-vin VIN                               # Decode a VIN (prints result)

# ── Maintenance ──
./oemparts cleanup --archive-older-than-days=180        # Archive old listings
./oemparts cleanup --mark-inactive                      # Scan for expired listings
./oemparts health                                       # Print system health JSON

# ── Admin / Dev ──
./oemparts user-create --email=... --tier=free          # Manually create a user (dev/admin)
./oemparts user-disable --user-id=U                     # Disable a user account
./oemparts token-refresh                                # Force eBay OAuth token refresh (debugging)
./oemparts dry-run-fetch --search-id=N                  # Fetch + print, don't persist (testing)

# ── Reporting (Phase 3+) ──
./oemparts epn-report --start=YYYY-MM-DD --end=YYYY-MM-DD  # EPN commission summary
```

---

## 13. Monetization Strategy

### Current: eBay Partner Network (EPN)

**How it works in practice:**
1. Owner joins EPN at partnernetwork.ebay.com and receives a Campaign ID (10-digit integer).
2. Campaign ID stored in `EPN_CAMPAIGN_ID` env var.
3. For every Browse API call, the request header `X-EBAY-C-ENDUSERCTX` includes `affiliateCampaignId={EPN_CAMPAIGN_ID}`.
4. The API response includes `itemAffiliateWebUrl` on each listing, which is the pre-constructed affiliate URL.
5. Dashboard renders listings with `itemAffiliateWebUrl` as the click-through target.
6. When a user clicks through and completes a qualifying purchase within 24 hours, EPN records a commission.

**Commission economics:** 1–4% of sale price depending on category, with per-transaction earnings caps. Auto parts typically fall in the 1-2% range. Realistic expectation for Phase 1-2: negligible (single-digit dollars/month during personal use). Realistic for Phase 3+: covers hosting costs at best, unless user count scales significantly. See [Section 10](#10-phase-roadmap) Phase 4 exit criteria.

**Reporting integration (Phase 3):** EPN provides a reporting API separate from Browse. A scheduled job can pull daily commission reports into an internal `epn_events` table for the owner's dashboard view. Not user-facing.

### Not in Scope (Initial)
- Paid subscriptions (deferred to Phase 4, conditional on user demand)
- Sponsored listings from parts vendors (deferred indefinitely; would change the product character)
- Ads (never — would degrade UX and compete with EPN clicks)

---

## 14. Legal & Compliance

This section exists because going from "hobby tool for myself" to "website other people use" crosses a legal threshold, and being casual about it is how small projects become big problems.

### eBay Developer Agreement
The Browse API is accessible under a basic developer keyset in sandbox immediately, and in production with reasonable use. Scaling beyond personal use — particularly with affiliate monetization — requires completing eBay's production application review. This must be done before public launch (Phase 3 gate). The review covers data handling, retention, display requirements (eBay branding, legal disclaimers), and affiliate compliance.

### eBay Partner Network Rules
EPN affiliate links may only appear on external properties (our dashboard at oempartsagent.com). They may not be posted on eBay itself or in eBay Messages. All pages displaying affiliate content must include clear affiliate disclosure (e.g., "As an eBay Partner, we may earn commissions from qualifying purchases"). The disclosure wording and placement must satisfy FTC requirements.

### Data Retention
eBay's developer terms constrain how long we can cache listing data. The `listings` table is our local cache; retention policy follows `LISTING_ARCHIVE_AFTER_DAYS` (default 180 days). Price history is considered derived data and may be retained longer under research/analytics use — this boundary should be confirmed with the eBay terms at Phase 3 review.

### User Data & Privacy
Users store email (from Clerk) and vehicle data (Y/M/M, VIN). VINs are potentially identifying — a VIN plus zip code can identify an individual. Treat VIN as sensitive; don't log it in application logs, don't include it in error messages sent to third-party error trackers, don't expose it in any public-facing API. Privacy policy required before Phase 3 launch.

### Jurisdiction
Initial launch US-only (`EBAY_MARKETPLACE_ID=EBAY_US`, NHTSA VIN decode is US-specific). International support deferred — would require UK/EU marketplace support, VIN decoding alternatives (e.g., vinbasic or paid services for non-US), and GDPR compliance.

---

## 15. What's Next

### Completed
- **Phase 0:** Specification, scaffolding, eBay developer account, domain acquisition
- **Phase 1:** Full single-user MVP — OAuth, Browse API, search runner, dedup, CLI, dashboard (4 pages), condition filtering, auto-fetch, 19 passing tests

### Next Up (Phase 2 — multi-tenancy + deployment)
- Clerk auth integration (swap `basic.py` for `clerk.py`)
- EC2 deployment with Docker Compose, Caddy reverse proxy, SSL for oempartsagent.com
- VIN decoding via NHTSA
- Y/M/M cascading dropdowns via Taxonomy API
- Taxonomy-based compatibility_filter (replace Phase 1 query enrichment workaround)
- Cron-driven fetch cycles (currently CLI-only)
- Commit and push Phase 1 code to GitHub

### Backlog (Phase 3+)
- Email alerts via AWS SES (price drops, new listings)
- EPN affiliate URL integration
- EPN commission reporting
- Health banner (pattern borrowed from StockAgent)
- System Status page

### Deferred (explicit non-goals for now)
- International marketplaces beyond EBAY_US
- Paid subscription tiers (Phase 4 conditional)
- Mobile app (web is responsive; native app requires separate justification)
- Integrations with non-eBay parts sources (RockAuto, dealer catalogs)
- ML-based price prediction (we track, we don't forecast)

---

## 16. Further Reading

| Document | Location | What It Covers |
|----------|----------|----------------|
| eBay Browse API docs | developer.ebay.com/api-docs/buy/browse/overview.html | Request/response structure, filters, compatibility |
| eBay OAuth guide | developer.ebay.com/api-docs/static/oauth-client-credentials-grant.html | Token management |
| eBay Partner Network | partnernetwork.ebay.com | EPN signup, campaign setup, rate card |
| NHTSA vPIC API | vpic.nhtsa.dot.gov/api | VIN decode endpoints |
| StockAgent SYSTEM_CONTEXT | (reference project) | Companion project; similar stack, operational patterns |
| Clerk docs | clerk.com/docs | Auth integration (Phase 2+) |

---

## Changelog

All significant changes to the system should be logged here. Format: `YYYY-MM-DD: Description`.

| Date | Change | Details |
|------|--------|---------|
| 2026-04-21 | **SYSTEM_CONTEXT.md created** | Initial specification document (Phase 0). Project conceived as personal parts tracker for 2012 Land Rover LR4, architected for commercial multi-tenant expansion. Domain oempartsagent.com acquired. Stack decision: FastAPI + HTMX + Jinja2 + PostgreSQL + Docker Compose on AWS EC2, mirroring StockAgent operational patterns. Monetization via eBay Partner Network only. Phase 0 gate: spec sign-off, repo scaffolding, credentials acquisition. |
| 2026-04-21 | **Phase 1 scaffolding created** | Full project skeleton per Section 3. SQLAlchemy models for all 12 tables (Section 7). Initial Alembic migration (`862710ad10dd`) generated and verified (upgrade/downgrade/upgrade). `docker-compose.local.yml` for local Postgres 15 on port 5442. `.env.example` aligned to Section 11 (replaced StockAgent template). `requirements.txt` with initial dependencies. `app/config.py` with Pydantic Settings. No application logic — skeleton only. Note: `user_preferences` and `subscriptions` (mentioned in Section 6) not modeled; they are not defined in Section 7 and are deferred to later phases. |
| 2026-04-28 | **OEM-only title filter** | Added `oem_only` boolean column to `searches` (migration `50028b2596f2`), defaulting to `false` for existing rows and to `true` for newly created searches that include an OEM number. New module `app/core/oem_filter.py` keeps a listing only if its title contains the word "OEM", the word "Genuine", or the normalized OEM part number itself (with hyphens/spaces stripped, ≥4 chars to avoid collisions). Filter applied post-fetch in `search_runner.py` because the eBay Browse API has no native title-must-contain filter. Web UI: new "OEM-only" column in the searches table with a per-row On/Off toggle button (`PATCH /searches/{id}/toggle-oem-only`). 14 new unit tests; 33 tests passing total. |
| 2026-04-26 | **Phase 1 MVP complete** | Full single-user MVP implemented and validated with real eBay data. **Database:** `session.py` (get_db + get_session), `queries.py` (25+ functions), `seed_dev_user.py`. Migration `2ef816902a3e` adds `condition_filter` to searches table. **Auth:** HTTP Basic with session cookie (`basic.py`, `dependencies.py`), auto-creates user on first login. **eBay integration:** OAuth client-credentials with 3-tier caching (`ebay_oauth.py`), Browse API search with condition filtering (`ebay_browse.py`). Note: `compatibility_filter` requires leaf-level category IDs — Phase 1 workaround prepends vehicle year/make/model to query text. **Business logic:** `search_runner.py`, `deduplicator.py` (TTL-based), `price_tracker.py`, `compatibility.py`. **CLI:** `./oemparts fetch/cleanup/health` via argparse. Manual cycles bypass dedup. **Dashboard:** Dark-themed FastAPI + HTMX. Pages: Home (stats), Vehicles (CRUD), Searches (CRUD + "Fetch Now" button + auto-fetch on create + condition filter), Listings (filters + pagination), Price History. HTMX partials for inline updates. **Tests:** 19 passing (auth, compatibility, dedup, queries, routes) against dedicated `oemparts_test` database. **Validation:** 249 real eBay listings fetched across 5 searches for 2012 Land Rover LR4. |
