# Price Intelligence + Morning Digest — Design

**Date:** 2026-08-24
**Status:** Spec pending owner review
**Phase:** 2/3 bridge (pulls Phase 3's alert concept forward in local-first form; owner-approved direction)

---

## 1. Problem

OEMParts now collects fitment-verified listings and price history automatically
(scheduled fetches, merged 2026-08-24), but it doesn't *judge* what it
collects, and it doesn't *tell* the owner anything:

1. **No price judgment.** Looking at a $230.65 compressor listing, the
   dashboard gives no signal whether that's a bargain or average. Months of
   `price_history` data go unused.
2. **No notification.** The nightly fetch found a $77.71 → $48.00 price drop
   on 2026-08-24 and logged it to a file nobody reads. Deals on eBay expire;
   a tracker that doesn't reach out is a diary, not a scout.

This spec adds both: per-search price statistics with deal flags in the
dashboard, and a morning email digest ("3 new listings, 1 price drop worth
seeing") sent to the owner's Gmail — readable on their phone, which is where
time-sensitive deals need to land. The owner chose email over macOS-only
notifications for exactly that reason.

## 2. Goals and non-goals

**Goals**

- Two deal signals, computed from existing data:
  - **Price drop:** a listing's price fell vs its own recorded history.
  - **Low-in-search:** a listing sits in the cheapest quartile of its
    search's active listings (with honest framing — see §4.1).
- Dashboard surfacing: badges on listing rows; a median-price column on the
  searches table.
- A daily digest email at 07:00 local (after the 03:00 fetch cycle), sent
  only when there is something to report. Delivery via Gmail SMTP using the
  Python standard library — no new dependencies.
- Alert events recorded in the existing `alerts` table (built in Phase 1 for
  exactly this purpose; currently unused) with dedup so the same deal is not
  re-announced daily.
- Everything degrades gracefully: SMTP failure logs an error and retries at
  the next digest run; missing/insufficient price data simply produces no
  badge.

**Non-goals (explicitly out of scope)**

- AI-assisted relevance filtering (separate feature, discussed 2026-08-24;
  not designed here).
- EPN affiliate links in emails (Phase 3; emails use plain `item_url`).
- AWS SES (the SYSTEM_CONTEXT Phase 3 plan — Gmail SMTP is the local-first
  stand-in; the digest module's send function is the single point to swap
  later).
- Per-listing price charts (already exist on the Price History page).
- Push notifications / native app.
- Cross-part price comparison or "fair market value" claims (see §4.1).

## 3. Current state (verified 2026-08-24)

- `price_history` — one row per listing per fetch (listing_id, price,
  recorded_at). Populated since Phase 1.
- `app/core/price_tracker.py` — `record_price_and_detect_change` already
  detects and *logs* price changes at fetch time; nothing persists or
  surfaces them.
- `alerts` table — exists, empty, never written: id, user_id (FK), search_id
  (FK), listing_id (FK), alert_type (`price_drop`/`new_listing`),
  triggered_at, notified_at (nullable), channel (`email`/`none`). **No
  migration needed for this feature.**
- `listings.first_seen_at` / `is_active` — support "new since yesterday" and
  active-set queries.
- Scheduled jobs run via launchd (`scripts/scheduled_job.sh` +
  `install_schedules.sh`); adding a job type is a case-statement entry plus
  one plist.
- `app/config.py` has no SMTP/digest settings; `alerts_enabled: bool = False`
  exists and is unused — this feature will use it as the digest kill switch.

## 4. Design

### 4.1 Deal signals — `app/core/price_stats.py` (new)

Two signals, deliberately narrow:

**Price drop (high confidence).** Compare a listing's current price against
the **highest price recorded for that listing within a lookback window**
(prices are snapshotted on every fetch, so "last two rows" would miss a
drop followed by stable prices). If `current` is below that peak by at least
`DIGEST_PRICE_DROP_PCT` (default **10%**), it's a drop. This compares a
listing only against *itself*, so it is immune to the mixed-parts problem
below. Function:
`recent_drop(db, listing_id, lookback_days) -> PriceDrop | None` where
`PriceDrop = (old_price, new_price, pct)` — the dashboard badge uses
`lookback_days=7`; the digest uses `lookback_days=1` (its 24h window), so a
drop is announced once when it happens, not re-announced all week (the 7-day
alert dedup backstops this).

**Low-in-search (advisory).** A listing whose price is at or below the
**25th percentile** (`DEAL_PERCENTILE`, configurable) of its search's active
listings, computed only when the search has **≥ 5 active listings**.
Functions: `search_price_stats(db, search_id) -> SearchPriceStats | None`
(`median`, `minimum`, `count`, `p25`) and
`is_low_in_search(price, stats) -> bool`.

> **Honesty constraint — the mixed-parts problem.** A search like "AMK air
> suspension compressor" legitimately contains $25 bracket kits and $1,189
> compressors; a median over that set is not a market price for anything.
> Therefore the low-in-search signal is always presented as **"among the
> cheapest in this search"** — a relative position — never as "below market
> value". The price-drop signal is the trustworthy one; low-in-search is a
> browsing aid. UI copy and email copy must respect this framing.

All money math uses `decimal.Decimal` end to end. Percentile: nearest-rank on
the sorted active prices (simple, no interpolation — this is a browsing aid,
not statistics homework).

### 4.2 Dashboard surfacing

- **Listings page rows:** two optional badges — `▼ 18%` (price drop within
  the last 7 days, red-to-green styling per the dark theme) and `low`
  ("among the cheapest in this search", subdued styling). Computed at render
  time by the route via `price_stats` (cheap: one query for the search's
  active prices + the listing's last two history rows; the listings page is
  already per-search-filterable).
- **Searches table:** one new read-only column, **Median $** (with count in
  the tooltip: "median of 18 active listings"), from `search_price_stats`.
  Shows "—" below the 5-listing minimum.

No new routes; existing `listings` and `searches` routes pass extra context.
(The searches table gained its Category column the same way last week.)

### 4.3 Alert recording — reusing the Phase 1 `alerts` table

During the digest run (not at fetch time — keeps the fetch path untouched),
for each of the owner's active searches:

- **price_drop alert:** for each linked active listing with a
  `recent_drop` whose drop occurred since the last digest window.
- **new_listing alert:** for each linked listing with
  `first_seen_at` in the window **and** (`is_low_in_search` **or** title
  contains the search's OEM number). Plain new arrivals are *counted* in the
  email but do not get alert rows — otherwise a broad search would generate
  40 alerts/day of noise.

**Dedup rule:** skip creating an alert if one with the same
(search_id, listing_id, alert_type) exists with `triggered_at` in the last
**7 days**. A listing that keeps dropping still re-alerts weekly.

Rows are created with `channel='email'`, `notified_at=NULL`; `notified_at`
is stamped only after the SMTP send succeeds. Unstamped rows are picked up
by the next digest run — failed sends retry automatically with no extra
machinery.

**Multi-tenancy:** alerts carry `user_id`; the digest iterates users who have
active searches and builds each user's digest from their own searches only
(standard `.filter(user_id == ...)` contract). Recipient address: single
`DIGEST_TO` env var for now (single-user reality); noted as the thing that
becomes `users.email` when friends join.

### 4.4 Digest builder + email — `app/core/digest.py` (new)

```python
def build_digest(db: Session, user_id: UUID, window_start: datetime) -> DigestData | None
def render_digest(digest: DigestData) -> tuple[str, str]   # (plain_text, html)
def send_digest_email(subject: str, text: str, html: str) -> bool
def run_digest(db: Session) -> DigestSummary               # orchestrator, called by CLI
```

- **Window:** `window_start` = 24 hours before now. Combined with the alert
  dedup rule this is idempotent enough — running the digest twice in a day
  produces one email's worth of alerts, and the second run finds nothing
  new to say (all alerts stamped) so sends nothing.
- **Email content** (per search, capped at 5 detail lines each, counts for
  the rest): price drops ("Ignition coil 8-pack — $77.71 → **$48.00**
  (−38%)"), notable new listings with price and the low/OEM-match reason,
  plus a one-line summary of totals ("14 other new listings across 4
  searches"). Every listing links via its stored `item_url`. Subject line:
  "OEMParts: 1 price drop, 3 notable listings". **No email when there is
  nothing to report** — silence means nothing happened, which keeps the
  signal trustworthy.
- **HTML + plain-text multipart** via stdlib `email.message.EmailMessage`;
  minimal inline styling (email clients ignore stylesheets). No VINs, no
  secrets in email content. SMTP errors: log at `error`, return False, leave
  `notified_at` NULL.
- **Send:** stdlib `smtplib`, `smtp.gmail.com:587`, STARTTLS, login with a
  Gmail **app password** (never the account password).

### 4.5 Configuration (`app/config.py` + `.env.example`)

```bash
# ── Digest / Alerts ──
ALERTS_ENABLED=true                  # existing flag, now the digest switch
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=dennfish@gmail.com
SMTP_PASSWORD=<gmail-app-password>   # Google Account → Security → App passwords
DIGEST_FROM=dennfish@gmail.com
DIGEST_TO=dennfish@gmail.com
DIGEST_PRICE_DROP_PCT=10             # min % drop to alert
DEAL_PERCENTILE=25                   # "low in search" threshold
```

`ALERTS_ENABLED=false` (the default) makes `oemparts digest` compute and
print a summary without sending — safe in CI and for anyone without SMTP
configured. Owner setup is a 2-minute task: create the app password
(requires 2FA on the Google account), paste into `.env`.

### 4.6 CLI + schedule

- `oemparts digest` — new subcommand: runs `run_digest`, prints a summary
  table (alerts created, email sent or skipped and why). Follows the
  `taxonomy-sync` pattern.
- `scripts/scheduled_job.sh` gains a `digest` job type;
  `install_schedules.sh` adds `com.mrdenfish.oemparts.digest` at **07:00
  local** daily. 07:00 rather than right after the 03:00 fetch so a Mac
  asleep at 3 (whose fetch fires on wake) still usually fetches before the
  digest, and the email arrives at coffee time rather than 3am.
- Digest run does not fetch; it reads whatever the night produced. If the
  Mac was off all night, the 07:00 digest simply reports the last window's
  findings or stays silent.

## 5. Testing

All against `oemparts_test`, hermetic wrt `.env` (settings monkeypatched;
SMTP never touched by the suite):

1. **price_stats:** drop detection (threshold boundary, single-history-row
   listings → None), nearest-rank percentile, stats below the 5-listing
   minimum → None, Decimal throughout.
2. **Alert recording:** drop and notable-new alerts created with correct
   user/search/listing; plain new listings counted not alerted; 7-day dedup;
   user-scoping (two users, no cross-talk — multi-tenancy contract test).
3. **Digest builder/renderer:** caps at 5 lines per search; totals line;
   empty digest → None (no email); subject line contents; no unstamped-alert
   loss on simulated send failure (notified_at stays NULL, next run retries).
4. **Send:** `smtplib.SMTP` monkeypatched; success stamps `notified_at`,
   failure doesn't; `ALERTS_ENABLED=false` short-circuits before SMTP.
5. **Routes/UI:** listings page renders drop badge and low badge from
   fixture data; searches page renders Median column; sub-minimum search
   shows "—".
6. **CLI:** digest subcommand wired, prints summary (orchestrator mocked).

**Live validation (manual, with owner):** owner creates the Gmail app
password; run `oemparts digest` by hand; confirm the email arrives on the
phone and reads well; verify badges against the real LR4 data; then let the
schedule run overnight and check the 07:00 email next morning.

## 6. Rollout

1. Code + tests + config plumbing merge (feature inert until
   `ALERTS_ENABLED=true` and SMTP creds exist).
2. Owner sets up the app password; manual digest run end-to-end.
3. Install the 07:00 launchd job; overnight soak.
4. SYSTEM_CONTEXT.md changelog + §15 updates when shipped.

Risk is bounded: no schema changes, no fetch-path changes, kill switch
defaults off, and a failed email leaves alerts queued for retry.
