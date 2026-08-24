# Price Intelligence + Morning Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deal signals (price drops, low-in-search) surfaced as dashboard badges plus a 07:00 email digest, reusing the empty Phase 1 `alerts` table.

**Architecture:** A pure-computation stats module (`app/core/price_stats.py`) feeds both surfaces. The digest orchestrator (`app/core/digest.py`) scans the last 24h per user, records deduped alert rows, renders a multipart email from all unnotified alerts, sends via Gmail SMTP (stdlib `smtplib`), and stamps `notified_at` only on send success — failed sends retry automatically next run. Routes pass computed stats/badges into existing templates; a new `digest` launchd job runs daily at 07:00.

**Tech Stack:** Python 3.11, SQLAlchemy 2.x sync, FastAPI + Jinja2/HTMX, stdlib `smtplib`/`email.message`, launchd. **No new dependencies, no schema changes.**

**Spec:** `docs/superpowers/specs/2026-08-24-price-intelligence-digest-design.md`

## Global Constraints

- Money is `decimal.Decimal` end to end; timestamps UTC-aware via `app.db.models.utcnow`.
- Thresholds: `DIGEST_PRICE_DROP_PCT` default **10**, `DEAL_PERCENTILE` default **25**, low-in-search requires **≥ 5 active listings**, alert dedup window **7 days**, digest window **24 hours**, detail cap **5 lines per search**.
- Low-in-search copy is always relative ("among the cheapest in this search") — never "below market value".
- No email when there is nothing to report. `ALERTS_ENABLED=false` (existing setting, default false) computes but never sends.
- SMTP failure: log at `error`, leave `notified_at` NULL. Never log `smtp_password`. No VINs in emails.
- User-scoped queries filter by `user_id` (multi-tenancy contract); digest iterates users independently.
- Type hints everywhere; `mypy` clean on new modules; tests hermetic wrt `.env` (monkeypatch `settings`); suite runs against `oemparts_test` via existing conftest.
- Before each commit: `.venv/bin/ruff check . && .venv/bin/ruff format .` (ruff.toml already excludes alembic from formatting).
- Commit messages: imperative present tense.
- Local Postgres: `docker compose -f docker-compose.local.yml up -d db` if not running. Tests: `.venv/bin/pytest`.

---

### Task 1: Price stats module + config thresholds

**Files:**
- Create: `app/core/price_stats.py`
- Modify: `app/config.py` (add two settings in the Fetching/Alerts area)
- Test: `tests/core/test_price_stats.py`

**Interfaces:**
- Consumes: `PriceHistory`, `Listing`, `SearchListing` models; `settings.digest_price_drop_pct`.
- Produces (used by Tasks 3 and 5):
  - `@dataclass PriceDrop(old_price: Decimal, new_price: Decimal, pct: Decimal)`
  - `@dataclass SearchPriceStats(median: Decimal, minimum: Decimal, p25: Decimal, count: int)`
  - `recent_drop(db: Session, listing_id: UUID, lookback_days: int) -> PriceDrop | None`
  - `search_price_stats(db: Session, search_id: UUID) -> SearchPriceStats | None`
  - `is_low_in_search(price: Decimal, stats: SearchPriceStats | None) -> bool`
  - `MIN_LISTINGS_FOR_STATS = 5`

- [ ] **Step 1: Add settings**

In `app/config.py`, directly under the `# Alerts (Phase 3)` section's `alerts_enabled` line, add:

```python
    digest_price_drop_pct: int = 10
    deal_percentile: int = 25
```

- [ ] **Step 2: Write the failing tests**

Create `tests/core/test_price_stats.py`:

```python
"""Tests for price statistics and deal signals."""

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.price_stats import (
    MIN_LISTINGS_FOR_STATS,
    is_low_in_search,
    recent_drop,
    search_price_stats,
)
from app.db.models import Listing, PriceHistory, Search, SearchListing, utcnow


def make_listing(db: Session, price: str, item_id: str | None = None) -> Listing:
    listing = Listing(
        ebay_item_id=item_id or f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Test part",
        price=Decimal(price),
        item_url="https://www.ebay.com/itm/1",
    )
    db.add(listing)
    db.flush()
    return listing


def snapshot(db: Session, listing: Listing, price: str, days_ago: float) -> None:
    db.add(
        PriceHistory(
            listing_id=listing.id,
            price=Decimal(price),
            recorded_at=utcnow() - timedelta(days=days_ago),
        )
    )
    db.flush()


def link(db: Session, search: Search, listing: Listing) -> None:
    db.add(SearchListing(search_id=search.id, listing_id=listing.id))
    db.flush()


class TestRecentDrop:
    def test_drop_vs_window_peak(self, db_session: Session) -> None:
        listing = make_listing(db_session, "48.00")
        snapshot(db_session, listing, "77.71", days_ago=3)
        snapshot(db_session, listing, "77.71", days_ago=2)
        snapshot(db_session, listing, "48.00", days_ago=0.1)
        drop = recent_drop(db_session, listing.id, lookback_days=7)
        assert drop is not None
        assert drop.old_price == Decimal("77.71")
        assert drop.new_price == Decimal("48.00")
        assert drop.pct == Decimal("38")  # rounded to whole percent

    def test_below_threshold_is_not_a_drop(self, db_session: Session) -> None:
        listing = make_listing(db_session, "95.00")
        snapshot(db_session, listing, "100.00", days_ago=1)
        snapshot(db_session, listing, "95.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None  # 5% < 10%

    def test_exactly_threshold_is_a_drop(self, db_session: Session) -> None:
        listing = make_listing(db_session, "90.00")
        snapshot(db_session, listing, "100.00", days_ago=1)
        snapshot(db_session, listing, "90.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is not None

    def test_peak_outside_lookback_ignored(self, db_session: Session) -> None:
        listing = make_listing(db_session, "48.00")
        snapshot(db_session, listing, "77.71", days_ago=10)  # outside 7-day window
        snapshot(db_session, listing, "48.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None

    def test_single_snapshot_is_none(self, db_session: Session) -> None:
        listing = make_listing(db_session, "48.00")
        snapshot(db_session, listing, "48.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None

    def test_price_rise_is_none(self, db_session: Session) -> None:
        listing = make_listing(db_session, "100.00")
        snapshot(db_session, listing, "80.00", days_ago=1)
        snapshot(db_session, listing, "100.00", days_ago=0.1)
        assert recent_drop(db_session, listing.id, lookback_days=7) is None


class TestSearchPriceStats:
    def test_stats_computed(self, db_session: Session, test_search: Search) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]:
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        stats = search_price_stats(db_session, test_search.id)
        assert stats is not None
        assert stats.count == 5
        assert stats.minimum == Decimal("25.00")
        assert stats.median == Decimal("100.00")   # nearest-rank 50th of 5
        assert stats.p25 == Decimal("30.00")       # nearest-rank 25th of 5

    def test_below_minimum_count_is_none(
        self, db_session: Session, test_search: Search
    ) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00"]:  # only 4
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        assert MIN_LISTINGS_FOR_STATS == 5
        assert search_price_stats(db_session, test_search.id) is None

    def test_inactive_listings_excluded(
        self, db_session: Session, test_search: Search
    ) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]:
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        dead = make_listing(db_session, "1.00")
        dead.is_active = False
        link(db_session, test_search, dead)
        db_session.flush()
        stats = search_price_stats(db_session, test_search.id)
        assert stats is not None
        assert stats.count == 5
        assert stats.minimum == Decimal("25.00")


class TestIsLowInSearch:
    def test_low_and_not_low(self, db_session: Session, test_search: Search) -> None:
        for p in ["25.00", "30.00", "100.00", "200.00", "1000.00"]:
            listing = make_listing(db_session, p)
            link(db_session, test_search, listing)
        stats = search_price_stats(db_session, test_search.id)
        assert is_low_in_search(Decimal("28.00"), stats) is True
        assert is_low_in_search(Decimal("30.00"), stats) is True   # == p25
        assert is_low_in_search(Decimal("31.00"), stats) is False
        assert is_low_in_search(Decimal("28.00"), None) is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/core/test_price_stats.py -v`
Expected: ImportError — `app.core.price_stats` does not exist.

- [ ] **Step 4: Implement `app/core/price_stats.py`**

```python
"""Price statistics and deal signals.

Two deliberately narrow signals (spec §4.1):
  - recent_drop: a listing's current price vs its own peak within a lookback
    window. High confidence — a listing compared only against itself.
  - is_low_in_search: price at/below the 25th percentile of the search's
    active listings. Advisory only — searches legitimately mix part types
    ($25 brackets next to $1,189 compressors), so this is presented as
    "among the cheapest in this search", never "below market value".
"""

import math
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Listing, PriceHistory, SearchListing, utcnow

MIN_LISTINGS_FOR_STATS = 5


@dataclass
class PriceDrop:
    old_price: Decimal
    new_price: Decimal
    pct: Decimal  # whole-percent drop, e.g. Decimal("38")


@dataclass
class SearchPriceStats:
    median: Decimal
    minimum: Decimal
    p25: Decimal
    count: int


def recent_drop(
    db: Session, listing_id: uuid.UUID, lookback_days: int
) -> PriceDrop | None:
    """Return the drop from the window's peak price to the latest price.

    Prices are snapshotted on every fetch, so the comparison is latest
    snapshot vs the highest earlier snapshot within the window — "last two
    rows" would miss a drop followed by stable prices.
    """
    cutoff = utcnow() - timedelta(days=lookback_days)
    rows = (
        db.query(PriceHistory)
        .filter(
            PriceHistory.listing_id == listing_id,
            PriceHistory.recorded_at >= cutoff,
        )
        .order_by(PriceHistory.recorded_at.desc())
        .all()
    )
    if len(rows) < 2:
        return None

    current = rows[0].price
    peak = max(row.price for row in rows[1:])
    if peak <= 0 or current >= peak:
        return None

    pct = ((peak - current) / peak * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if pct < settings.digest_price_drop_pct:
        return None
    return PriceDrop(old_price=peak, new_price=current, pct=pct)


def _nearest_rank(sorted_prices: list[Decimal], percentile: int) -> Decimal:
    """Nearest-rank percentile: value at index ceil(P/100 * n) - 1."""
    index = math.ceil(percentile / 100 * len(sorted_prices)) - 1
    return sorted_prices[max(index, 0)]


def search_price_stats(
    db: Session, search_id: uuid.UUID
) -> SearchPriceStats | None:
    """Stats over a search's active listings; None below the minimum count."""
    prices = [
        row[0]
        for row in db.query(Listing.price)
        .join(SearchListing, SearchListing.listing_id == Listing.id)
        .filter(
            SearchListing.search_id == search_id,
            Listing.is_active.is_(True),
        )
        .all()
    ]
    if len(prices) < MIN_LISTINGS_FOR_STATS:
        return None
    prices.sort()
    return SearchPriceStats(
        median=_nearest_rank(prices, 50),
        minimum=prices[0],
        p25=_nearest_rank(prices, settings.deal_percentile),
        count=len(prices),
    )


def is_low_in_search(price: Decimal, stats: SearchPriceStats | None) -> bool:
    """True when price sits in the cheapest quartile of its search."""
    if stats is None:
        return False
    return price <= stats.p25
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/core/test_price_stats.py -v`
Expected: 11 PASS.

- [ ] **Step 6: Type-check, lint, full suite, commit**

```bash
.venv/bin/mypy app/core/price_stats.py
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format .
git add app/core/price_stats.py app/config.py tests/core/test_price_stats.py
git commit -m "Add price stats module: drop detection and low-in-search signals"
```

---

### Task 2: Alert query helpers

**Files:**
- Modify: `app/db/queries.py` (append a new "Alert queries" section at the end)
- Test: `tests/db/test_alert_queries.py`

**Interfaces:**
- Consumes: `Alert` model (existing, unused: user_id, search_id, listing_id, alert_type `price_drop`/`new_listing`, triggered_at, notified_at nullable, channel).
- Produces (used by Task 3):
  - `create_alert(db, user_id, search_id, listing_id, alert_type: str, channel: str = "email") -> Alert`
  - `recent_alert_exists(db, search_id, listing_id, alert_type: str, within_days: int = 7) -> bool`
  - `get_unnotified_alerts(db, user_id) -> list[Alert]` (ordered by triggered_at)
  - `mark_alerts_notified(db, alert_ids: list[uuid.UUID]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/db/test_alert_queries.py`:

```python
"""Tests for alert query helpers (digest dedup and retry semantics)."""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.db import queries
from app.db.models import Listing, Search, User, utcnow
from decimal import Decimal


def make_listing(db: Session) -> Listing:
    import uuid

    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title="Part",
        price=Decimal("10.00"),
        item_url="https://www.ebay.com/itm/1",
    )
    db.add(listing)
    db.flush()
    return listing


def test_create_and_fetch_unnotified(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    listing = make_listing(db_session)
    alert = queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "price_drop"
    )
    assert alert.notified_at is None
    pending = queries.get_unnotified_alerts(db_session, test_user.id)
    assert [a.id for a in pending] == [alert.id]


def test_mark_notified_removes_from_pending(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    listing = make_listing(db_session)
    alert = queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "new_listing"
    )
    queries.mark_alerts_notified(db_session, [alert.id])
    assert queries.get_unnotified_alerts(db_session, test_user.id) == []
    db_session.refresh(alert)
    assert alert.notified_at is not None


def test_recent_alert_dedup_window(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    listing = make_listing(db_session)
    alert = queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "price_drop"
    )
    assert queries.recent_alert_exists(
        db_session, test_search.id, listing.id, "price_drop"
    )
    # Different type is independent
    assert not queries.recent_alert_exists(
        db_session, test_search.id, listing.id, "new_listing"
    )
    # Age the alert past the window
    alert.triggered_at = utcnow() - timedelta(days=8)
    db_session.flush()
    assert not queries.recent_alert_exists(
        db_session, test_search.id, listing.id, "price_drop"
    )


def test_unnotified_alerts_are_user_scoped(
    db_session: Session, test_user: User, test_search: Search
) -> None:
    other = User(email="other@example.com")
    db_session.add(other)
    db_session.flush()
    listing = make_listing(db_session)
    queries.create_alert(
        db_session, test_user.id, test_search.id, listing.id, "price_drop"
    )
    assert queries.get_unnotified_alerts(db_session, other.id) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/db/test_alert_queries.py -v`
Expected: AttributeError — `queries.create_alert` does not exist.

- [ ] **Step 3: Implement in `app/db/queries.py`**

Append at the end of the file:

```python
# ---------------------------------------------------------------------------
# Alert queries (digest — spec docs/superpowers/specs/2026-08-24-*.md §4.3)
# ---------------------------------------------------------------------------


def create_alert(
    db: Session,
    user_id: uuid.UUID,
    search_id: uuid.UUID,
    listing_id: uuid.UUID,
    alert_type: str,
    channel: str = "email",
) -> Alert:
    """Record a digest alert. notified_at stays NULL until the email sends."""
    alert = Alert(
        user_id=user_id,
        search_id=search_id,
        listing_id=listing_id,
        alert_type=alert_type,
        channel=channel,
    )
    db.add(alert)
    db.flush()
    return alert


def recent_alert_exists(
    db: Session,
    search_id: uuid.UUID,
    listing_id: uuid.UUID,
    alert_type: str,
    within_days: int = 7,
) -> bool:
    """Dedup check: same (search, listing, type) alerted within the window."""
    cutoff = utcnow() - timedelta(days=within_days)
    return (
        db.query(Alert.id)
        .filter(
            Alert.search_id == search_id,
            Alert.listing_id == listing_id,
            Alert.alert_type == alert_type,
            Alert.triggered_at >= cutoff,
        )
        .first()
        is not None
    )


def get_unnotified_alerts(db: Session, user_id: uuid.UUID) -> list[Alert]:
    """All of a user's alerts that have not been emailed yet."""
    return (
        db.query(Alert)
        .filter(Alert.user_id == user_id, Alert.notified_at.is_(None))
        .order_by(Alert.triggered_at)
        .all()
    )


def mark_alerts_notified(db: Session, alert_ids: list[uuid.UUID]) -> None:
    """Stamp notified_at after a successful email send."""
    if not alert_ids:
        return
    db.execute(
        update(Alert).where(Alert.id.in_(alert_ids)).values(notified_at=utcnow())
    )
    db.flush()
```

Add the needed imports at the top of `queries.py` if absent: `Alert` in the
`from app.db.models import ...` line, and `from datetime import timedelta`
(`update` and `utcnow` are already imported).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/db/ -v`
Expected: new tests PASS, existing `test_queries.py` still green.

- [ ] **Step 5: Lint, commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format .
git add app/db/queries.py tests/db/test_alert_queries.py
git commit -m "Add alert query helpers for the digest"
```

---

### Task 3: Digest module (scan, record, render, send)

**Files:**
- Create: `app/core/digest.py`
- Modify: `app/config.py` (SMTP/digest settings), `.env.example` (document them)
- Test: `tests/core/test_digest.py`

**Interfaces:**
- Consumes: Task 1 (`recent_drop`, `search_price_stats`, `is_low_in_search`, `PriceDrop`), Task 2 alert helpers, `queries.get_searches_for_user`, `queries.get_listings_for_search`, `app.core.oem_filter.title_matches_oem`, `User` model.
- Produces (used by Task 4):
  - `run_digest(db: Session) -> DigestSummary`
  - `@dataclass DigestSummary(alerts_created: int, emails_sent: int, skipped_reason: str | None)`
  - Internal (tested directly): `scan_and_record(db, user_id, window_start) -> tuple[int, dict[uuid.UUID, int]]` (alerts created, other-new counts per search), `render_digest(db, alerts, other_new) -> tuple[str, str, str]` (subject, text, html), `send_email(subject, text, html) -> bool`.

- [ ] **Step 1: Add settings**

In `app/config.py`, replace the `# Alerts (Phase 3)` comment with `# Alerts / Digest` and extend the block (keeping `alerts_enabled`, `digest_price_drop_pct`, `deal_percentile` from Task 1):

```python
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    digest_from: str = ""
    digest_to: str = ""
```

In `.env.example`, replace the existing `# ── Alerts (Phase 3) ──` block with:

```bash
# ── Alerts / Morning digest ──
ALERTS_ENABLED=false                 # true = send the daily email digest
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=change_me              # Gmail app password (Google Account -> Security -> App passwords)
DIGEST_FROM=you@gmail.com
DIGEST_TO=you@gmail.com
DIGEST_PRICE_DROP_PCT=10             # min % drop to alert
DEAL_PERCENTILE=25                   # "low in search" threshold
```

(Keep the `AWS_SES_*` lines out — SES is a Phase 3 concern; delete them from `.env.example` if present there under the old Alerts block.)

- [ ] **Step 2: Write the failing tests**

Create `tests/core/test_digest.py`:

```python
"""Tests for the morning digest: scan/record, render, send semantics."""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.core import digest
from app.db import queries
from app.db.models import (
    Listing,
    PriceHistory,
    Search,
    SearchListing,
    User,
    utcnow,
)


@pytest.fixture(autouse=True)
def _digest_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "alerts_enabled", True)
    monkeypatch.setattr(settings, "smtp_username", "test@example.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(settings, "digest_from", "test@example.com")
    monkeypatch.setattr(settings, "digest_to", "test@example.com")


def make_listing(
    db: Session, price: str, title: str = "Part", days_old: float = 0.1
) -> Listing:
    listing = Listing(
        ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
        title=title,
        price=Decimal(price),
        item_url="https://www.ebay.com/itm/1",
    )
    db.add(listing)
    db.flush()
    listing.first_seen_at = utcnow() - timedelta(days=days_old)
    db.flush()
    return listing


def link(db: Session, search: Search, listing: Listing) -> None:
    db.add(SearchListing(search_id=search.id, listing_id=listing.id))
    db.flush()


def snapshot(db: Session, listing: Listing, price: str, days_ago: float) -> None:
    db.add(
        PriceHistory(
            listing_id=listing.id,
            price=Decimal(price),
            recorded_at=utcnow() - timedelta(days=days_ago),
        )
    )
    db.flush()


def seed_baseline(db: Session, search: Search) -> None:
    """Five old, mid-priced listings so stats exist and nothing is 'new'."""
    for p in ["100.00", "110.00", "120.00", "130.00", "140.00"]:
        listing = make_listing(db, p, days_old=10)
        link(db, search, listing)


class TestScanAndRecord:
    def test_price_drop_creates_alert(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        seed_baseline(db_session, test_search)
        dropped = make_listing(db_session, "48.00", days_old=10)
        link(db_session, test_search, dropped)
        snapshot(db_session, dropped, "77.71", days_ago=0.9)
        snapshot(db_session, dropped, "48.00", days_ago=0.05)

        created, _ = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 1
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        assert alerts[0].alert_type == "price_drop"
        assert alerts[0].listing_id == dropped.id

    def test_notable_new_listing_low_in_search(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        seed_baseline(db_session, test_search)
        cheap_new = make_listing(db_session, "50.00", days_old=0.1)  # below p25=110
        link(db_session, test_search, cheap_new)

        created, other = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 1
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        assert alerts[0].alert_type == "new_listing"

    def test_new_listing_with_oem_number_is_notable(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        # test_search fixture has oem_number LR010819
        seed_baseline(db_session, test_search)
        oem_new = make_listing(
            db_session, "500.00", title="Hose LR010819 genuine", days_old=0.1
        )
        link(db_session, test_search, oem_new)
        created, _ = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 1

    def test_plain_new_listing_counted_not_alerted(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        seed_baseline(db_session, test_search)
        plain_new = make_listing(db_session, "125.00", days_old=0.1)  # mid-priced
        link(db_session, test_search, plain_new)
        created, other = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 0
        assert other[test_search.id] == 1

    def test_dedup_no_second_alert(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        seed_baseline(db_session, test_search)
        dropped = make_listing(db_session, "48.00", days_old=10)
        link(db_session, test_search, dropped)
        snapshot(db_session, dropped, "77.71", days_ago=0.9)
        snapshot(db_session, dropped, "48.00", days_ago=0.05)
        window = utcnow() - timedelta(days=1)
        first, _ = digest.scan_and_record(db_session, test_user.id, window)
        second, _ = digest.scan_and_record(db_session, test_user.id, window)
        assert first == 1
        assert second == 0


class TestRenderAndSend:
    def _pending(self, db: Session, user: User, search: Search) -> list:
        seed_baseline(db, search)
        dropped = make_listing(db, "48.00", title="Coil pack", days_old=10)
        link(db, search, dropped)
        snapshot(db, dropped, "77.71", days_ago=0.9)
        snapshot(db, dropped, "48.00", days_ago=0.05)
        digest.scan_and_record(db, user.id, utcnow() - timedelta(days=1))
        return queries.get_unnotified_alerts(db, user.id)

    def test_render_contents(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        alerts = self._pending(db_session, test_user, test_search)
        subject, text, html = digest.render_digest(db_session, alerts, {})
        assert "1 price drop" in subject
        assert "Coil pack" in text
        assert "48.00" in text and "77.71" in text
        assert "cheapest in this search" not in text  # no low items in this fixture
        assert "https://www.ebay.com/itm/1" in html

    def test_run_digest_sends_and_stamps(
        self,
        db_session: Session,
        test_user: User,
        test_search: Search,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._pending(db_session, test_user, test_search)
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(digest, "send_email", sent)
        summary = digest.run_digest(db_session)
        assert summary.emails_sent == 1
        assert sent.call_count == 1
        assert queries.get_unnotified_alerts(db_session, test_user.id) == []

    def test_send_failure_leaves_alerts_pending(
        self,
        db_session: Session,
        test_user: User,
        test_search: Search,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._pending(db_session, test_user, test_search)
        monkeypatch.setattr(digest, "send_email", MagicMock(return_value=False))
        summary = digest.run_digest(db_session)
        assert summary.emails_sent == 0
        assert len(queries.get_unnotified_alerts(db_session, test_user.id)) == 1

    def test_nothing_to_report_sends_nothing(
        self, db_session: Session, test_user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(digest, "send_email", sent)
        summary = digest.run_digest(db_session)
        assert summary.emails_sent == 0
        assert sent.call_count == 0

    def test_alerts_disabled_short_circuits(
        self,
        db_session: Session,
        test_user: User,
        test_search: Search,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "alerts_enabled", False)
        self._pending(db_session, test_user, test_search)
        sent = MagicMock(return_value=True)
        monkeypatch.setattr(digest, "send_email", sent)
        summary = digest.run_digest(db_session)
        assert summary.skipped_reason == "alerts_disabled"
        assert sent.call_count == 0


class TestSendEmail:
    def test_smtp_wiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        smtp_instance = MagicMock()
        smtp_cls = MagicMock()
        smtp_cls.return_value.__enter__ = MagicMock(return_value=smtp_instance)
        smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(digest.smtplib, "SMTP", smtp_cls)
        assert digest.send_email("subj", "text", "<p>html</p>") is True
        smtp_instance.starttls.assert_called_once()
        smtp_instance.login.assert_called_once_with("test@example.com", "app-password")
        smtp_instance.send_message.assert_called_once()

    def test_smtp_error_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        smtp_cls = MagicMock(side_effect=OSError("connection refused"))
        monkeypatch.setattr(digest.smtplib, "SMTP", smtp_cls)
        assert digest.send_email("subj", "text", "<p>html</p>") is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/core/test_digest.py -v`
Expected: ImportError — `app.core.digest` does not exist.

- [ ] **Step 4: Implement `app/core/digest.py`**

```python
"""Morning digest: scan for deals, record alerts, email the owner.

Flow (spec §4.3-4.4): scan_and_record finds price drops (24h window) and
notable new listings per active search and writes deduped Alert rows with
notified_at NULL. run_digest then emails ALL of a user's unnotified alerts
(so a failed send retries next run) and stamps notified_at on success.
Plain new arrivals are counted in the email but never alerted — noise
control. No email when there is nothing to say.
"""

import logging
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.config import settings
from app.core.oem_filter import title_matches_oem
from app.core.price_stats import (
    is_low_in_search,
    recent_drop,
    search_price_stats,
)
from app.db import queries
from app.db.models import Alert, Listing, Search, User, utcnow

logger = logging.getLogger(__name__)

DETAIL_CAP_PER_SEARCH = 5
ALERT_DEDUP_DAYS = 7


@dataclass
class DigestSummary:
    alerts_created: int
    emails_sent: int
    skipped_reason: str | None = None


def scan_and_record(
    db: Session, user_id: uuid.UUID, window_start: datetime
) -> tuple[int, dict[uuid.UUID, int]]:
    """Create alert rows for the window. Returns (created, other_new_counts)."""
    created = 0
    other_new: dict[uuid.UUID, int] = {}

    for search in queries.get_searches_for_user(db, user_id):
        if not search.is_active:
            continue
        stats = search_price_stats(db, search.id)
        listings = queries.get_listings_for_search(db, search.id, active_only=True)

        for listing in listings:
            # Signal 1: price drop within the digest window (own history).
            drop = recent_drop(db, listing.id, lookback_days=1)
            if drop is not None and not queries.recent_alert_exists(
                db, search.id, listing.id, "price_drop", ALERT_DEDUP_DAYS
            ):
                queries.create_alert(
                    db, user_id, search.id, listing.id, "price_drop"
                )
                created += 1

            # Signal 2: new listing in the window — notable or just counted.
            if listing.first_seen_at >= window_start:
                notable = is_low_in_search(listing.price, stats) or (
                    search.oem_number is not None
                    and title_matches_oem(listing.title, search.oem_number)
                )
                if notable:
                    if not queries.recent_alert_exists(
                        db, search.id, listing.id, "new_listing", ALERT_DEDUP_DAYS
                    ):
                        queries.create_alert(
                            db, user_id, search.id, listing.id, "new_listing"
                        )
                        created += 1
                else:
                    other_new[search.id] = other_new.get(search.id, 0) + 1

    db.commit()
    return created, other_new


def _fmt(price) -> str:
    return f"${price:,.2f}"


def render_digest(
    db: Session, alerts: list[Alert], other_new: dict[uuid.UUID, int]
) -> tuple[str, str, str]:
    """Build (subject, plain_text, html) from pending alerts."""
    drops = [a for a in alerts if a.alert_type == "price_drop"]
    news = [a for a in alerts if a.alert_type == "new_listing"]

    parts = []
    if drops:
        parts.append(f"{len(drops)} price drop{'s' if len(drops) != 1 else ''}")
    if news:
        parts.append(
            f"{len(news)} notable listing{'s' if len(news) != 1 else ''}"
        )
    subject = "OEMParts: " + ", ".join(parts)

    # Group alerts per search, cap detail lines (spec: 5 per search).
    by_search: dict[uuid.UUID, list[Alert]] = {}
    for alert in alerts:
        by_search.setdefault(alert.search_id, []).append(alert)

    text_lines: list[str] = []
    html_lines: list[str] = ["<h2>OEMParts morning digest</h2>"]
    for search_id, search_alerts in by_search.items():
        search = db.get(Search, search_id)
        if search is None:
            continue
        text_lines.append(f"\n== {search.query_text} ==")
        html_lines.append(f"<h3>{search.query_text}</h3><ul>")
        for alert in search_alerts[:DETAIL_CAP_PER_SEARCH]:
            listing = db.get(Listing, alert.listing_id)
            if listing is None:
                continue
            if alert.alert_type == "price_drop":
                drop = recent_drop(db, listing.id, lookback_days=7)
                detail = (
                    f"{_fmt(drop.old_price)} -> {_fmt(drop.new_price)}"
                    f" (-{drop.pct}%)"
                    if drop
                    else _fmt(listing.price)
                )
                label = "PRICE DROP"
            else:
                detail = f"{_fmt(listing.price)} — new, among the cheapest in this search or matches your OEM number"
                label = "NEW"
            text_lines.append(f"[{label}] {listing.title[:70]} — {detail}")
            text_lines.append(f"    {listing.item_url}")
            html_lines.append(
                f'<li><b>{label}</b>: <a href="{listing.item_url}">'
                f"{listing.title[:70]}</a> — {detail}</li>"
            )
        hidden = len(search_alerts) - DETAIL_CAP_PER_SEARCH
        if hidden > 0:
            text_lines.append(f"    (+{hidden} more)")
            html_lines.append(f"<li>(+{hidden} more)</li>")
        html_lines.append("</ul>")

    total_other = sum(other_new.values())
    if total_other:
        summary = (
            f"{total_other} other new listing{'s' if total_other != 1 else ''} "
            f"across {len(other_new)} search{'es' if len(other_new) != 1 else ''}"
        )
        text_lines.append(f"\n{summary}")
        html_lines.append(f"<p>{summary}</p>")

    text_lines.append("\nOpen the dashboard: http://localhost:8000/listings")
    html_lines.append('<p><a href="http://localhost:8000/listings">Open the dashboard</a></p>')
    return subject, "\n".join(text_lines), "".join(html_lines)


def send_email(subject: str, text: str, html: str) -> bool:
    """Send via SMTP (STARTTLS). Returns False on any failure — never raises."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.digest_from
    message["To"] = settings.digest_to
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        logger.error("Digest email send failed: %s", exc)
        return False


def run_digest(db: Session) -> DigestSummary:
    """Scan, record, and email — one pass per user with active searches."""
    window_start = utcnow() - timedelta(days=1)
    total_created = 0
    emails_sent = 0

    users = db.query(User).all()
    for user in users:
        created, other_new = scan_and_record(db, user.id, window_start)
        total_created += created

        if not settings.alerts_enabled:
            continue

        pending = queries.get_unnotified_alerts(db, user.id)
        if not pending:
            continue

        subject, text, html = render_digest(db, pending, other_new)
        if send_email(subject, text, html):
            queries.mark_alerts_notified(db, [a.id for a in pending])
            db.commit()
            emails_sent += 1
        else:
            logger.error(
                "Digest for user %s not sent; %d alerts remain queued",
                user.id,
                len(pending),
            )

    skipped = "alerts_disabled" if not settings.alerts_enabled else None
    return DigestSummary(
        alerts_created=total_created,
        emails_sent=emails_sent,
        skipped_reason=skipped,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/core/test_digest.py -v`
Expected: 11 PASS.

- [ ] **Step 6: Type-check, lint, full suite, commit**

```bash
.venv/bin/mypy app/core/digest.py
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format .
git add app/core/digest.py app/config.py .env.example tests/core/test_digest.py
git commit -m "Add morning digest: deal scan, alert recording, email send"
```

---

### Task 4: CLI command + launchd schedule

**Files:**
- Modify: `app/worker/cli.py` (add `digest` subcommand), `scripts/scheduled_job.sh` (add `digest` job type), `scripts/install_schedules.sh` (add 07:00 agent), `scripts/uninstall_schedules.sh` (add `digest` to the removal list)
- Test: `tests/worker/test_cli_digest.py`

**Interfaces:**
- Consumes: Task 3 `run_digest(db) -> DigestSummary`.
- Produces: `./oemparts digest`; launchd agent `com.mrdenfish.oemparts.digest` at 07:00 local daily.

- [ ] **Step 1: Write the failing test**

Create `tests/worker/test_cli_digest.py`:

```python
"""CLI wiring test for the digest subcommand."""

from app.worker.cli import build_parser


def test_digest_subcommand_parses() -> None:
    args = build_parser().parse_args(["digest"])
    assert args.func.__name__ == "cmd_digest"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/worker/test_cli_digest.py -v`
Expected: FAIL — argparse error, unknown command `digest`.

- [ ] **Step 3: Implement CLI**

In `app/worker/cli.py`: update the module docstring command list to
`Commands: fetch, cleanup, health, taxonomy-sync, digest.` Add after
`cmd_taxonomy_sync`:

```python
def cmd_digest(args: argparse.Namespace) -> None:
    """Run the morning digest: scan for deals and email the summary."""
    setup_logging()
    from app.core.digest import run_digest

    with get_session() as db:
        summary = run_digest(db)
        # print() is acceptable here — CLI user-facing output (per CLAUDE.md)
        print(f"alerts created: {summary.alerts_created}")
        print(f"emails sent:    {summary.emails_sent}")
        if summary.skipped_reason:
            print(f"note: sending skipped ({summary.skipped_reason})")
```

In `build_parser()` before `return parser`:

```python
    # digest
    digest_parser = subparsers.add_parser(
        "digest", help="Scan for deals and email the morning digest"
    )
    digest_parser.set_defaults(func=cmd_digest)
```

- [ ] **Step 4: Wire the schedule**

`scripts/scheduled_job.sh` — add `digest` to both `case` statements:
the validation case becomes `nightly|intraday|cleanup|taxonomy-sync|digest)`,
and the dispatch case gains:

```bash
    digest)        "$VENV/bin/python" oemparts digest ;;
```

`scripts/install_schedules.sh` — add after the `taxonomy-sync` block, and
update the header comment's schedule list to include it:

```bash
write_plist "$PREFIX.digest" "digest" \
    '<dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>'
```

`scripts/uninstall_schedules.sh` — change the loop list to
`for job in nightly intraday cleanup taxonomy-sync digest; do`.

- [ ] **Step 5: Verify — tests, script syntax, reinstall schedules**

```bash
.venv/bin/pytest tests/worker/ -v
bash -n scripts/scheduled_job.sh scripts/install_schedules.sh scripts/uninstall_schedules.sh
./scripts/install_schedules.sh && launchctl list | grep -c oemparts   # expect 5
```

- [ ] **Step 6: Lint, commit**

```bash
.venv/bin/ruff check . && .venv/bin/ruff format .
git add app/worker/cli.py scripts/ tests/worker/test_cli_digest.py
git commit -m "Add digest CLI command and 07:00 launchd schedule"
```

---

### Task 5: Dashboard badges + median column

**Files:**
- Modify: `app/web/routes/listings.py`, `app/web/templates/components/listing_table.html`, `app/web/routes/searches.py`, `app/web/templates/components/search_row.html`, `app/web/templates/pages/searches.html`
- Test: `tests/web/test_price_badges.py`

**Interfaces:**
- Consumes: Task 1 (`recent_drop` lookback 7, `search_price_stats`, `is_low_in_search`).
- Produces: listings rows show `▼ N%` and `low` badges; searches table shows a Median $ column that survives HTMX row re-renders.

- [ ] **Step 1: Write the failing tests**

Create `tests/web/test_price_badges.py`:

```python
"""UI tests: price badges on listings, median column on searches."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Listing, PriceHistory, Search, SearchListing, utcnow


@pytest.fixture(autouse=True)
def _force_basic_auth(monkeypatch: pytest.MonkeyPatch):
    """Keep these tests hermetic regardless of the developer's .env."""
    monkeypatch.setattr(settings, "auth_backend", "basic")


def seed(db: Session, search: Search) -> Listing:
    """Five listings incl. one with a recorded price drop; returns the dropper."""
    prices = ["25.00", "110.00", "120.00", "130.00", "140.00"]
    listings = []
    for p in prices:
        listing = Listing(
            ebay_item_id=f"v1|{uuid.uuid4().hex[:12]}|0",
            title=f"Part at {p}",
            price=Decimal(p),
            item_url="https://www.ebay.com/itm/1",
        )
        db.add(listing)
        db.flush()
        db.add(SearchListing(search_id=search.id, listing_id=listing.id))
        listings.append(listing)
    dropper = listings[0]  # 25.00 — also the cheapest (low badge)
    db.add(
        PriceHistory(
            listing_id=dropper.id,
            price=Decimal("50.00"),
            recorded_at=utcnow() - timedelta(days=2),
        )
    )
    db.add(
        PriceHistory(
            listing_id=dropper.id,
            price=Decimal("25.00"),
            recorded_at=utcnow() - timedelta(hours=1),
        )
    )
    db.commit()
    return dropper


def test_listing_badges_render(
    client: TestClient, db_session: Session, test_search: Search
) -> None:
    seed(db_session, test_search)
    page = client.get(f"/listings/?search_id={test_search.id}")
    assert page.status_code == 200
    assert "▼ 50%" in page.text                      # drop badge
    assert "cheapest in this search" in page.text     # low badge tooltip


def test_searches_median_column(
    client: TestClient, db_session: Session, test_search: Search
) -> None:
    seed(db_session, test_search)
    page = client.get("/searches/")
    assert page.status_code == 200
    assert "<th>Median $</th>" in page.text
    assert "$120.00" in page.text


def test_median_dash_below_minimum(client: TestClient, test_search: Search) -> None:
    page = client.get("/searches/")
    assert page.status_code == 200
    assert "<th>Median $</th>" in page.text  # column exists even with no stats
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/web/test_price_badges.py -v`
Expected: FAIL — no badges/column in rendered pages.

- [ ] **Step 3: Listings route + template**

In `app/web/routes/listings.py` add imports:

```python
from app.core.price_stats import is_low_in_search, recent_drop, search_price_stats
```

In `listings_page`, after `listing_list = ...`, add:

```python
    # Deal badges (spec §4.2): drop = listing vs its own 7-day history;
    # low = cheapest quartile of the filtered search (only meaningful when
    # a single search is selected).
    stats = search_price_stats(db, search_id) if search_id else None
    listing_extras = {
        listing.id: {
            "drop": recent_drop(db, listing.id, lookback_days=7),
            "low": is_low_in_search(listing.price, stats),
        }
        for listing in listing_list
    }
```

and add `"listing_extras": listing_extras,` to the template context dict.

In `app/web/templates/components/listing_table.html`, inside the
`listing-meta` div (after the `Ended` badge block), add:

```html
            {% set extras = listing_extras.get(listing.id) if listing_extras else None %}
            {% if extras and extras.drop %}
                <span class="badge badge-active" title="Was {{ extras.drop.old_price|format_price }} within the last 7 days">▼ {{ extras.drop.pct }}%</span>
            {% endif %}
            {% if extras and extras.low %}
                <span class="badge" title="Among the cheapest in this search">low</span>
            {% endif %}
```

- [ ] **Step 4: Searches route + templates (HTMX-safe median)**

`search_row.html` is also rendered standalone by four HTMX endpoints, so the
median must come from every render site. In `app/web/routes/searches.py` add
the import:

```python
from app.core.price_stats import search_price_stats
```

and a helper above the routes:

```python
def _row_context(db: Session, search) -> dict:
    """Context for components/search_row.html — keeps HTMX row re-renders
    consistent with the full-page render (median column)."""
    return {"search": search, "price_stats": search_price_stats(db, search.id)}
```

Change every `templates.TemplateResponse(request, "components/search_row.html", {"search": search})`
call (in `create_search`, `toggle_search`, `toggle_search_oem_only`,
`fetch_search`) to pass `_row_context(db, search)` instead.

In `searches_page`, add to the context:

```python
            "search_stats": {
                s.id: search_price_stats(db, s.id) for s in user_searches
            },
```

In `app/web/templates/pages/searches.html`: add `<th>Median $</th>` after
`<th>Category</th>`, and where the page includes the row template inside its
loop, set the per-row variable first:

```html
                {% set price_stats = search_stats.get(search.id) %}
                {% include "components/search_row.html" %}
```

(If the page currently inlines `{% include "components/search_row.html" %}`
without a `set`, add the `set` line directly above it.)

In `app/web/templates/components/search_row.html`, add after the Category
`</td>`:

```html
    <td class="text-secondary">
        {% if price_stats %}
            <span title="Median of {{ price_stats.count }} active listings (min {{ price_stats.minimum|format_price }})">{{ price_stats.median|format_price }}</span>
        {% else %}
            —
        {% endif %}
    </td>
```

- [ ] **Step 5: Run tests — new file, then full suite**

```bash
.venv/bin/pytest tests/web/ -v
.venv/bin/pytest
```

Expected: all PASS (if `tests/web/test_routes.py` asserts column structure,
update it for the new column).

- [ ] **Step 6: Type-check, lint, commit**

```bash
.venv/bin/mypy app/web/routes/listings.py app/web/routes/searches.py
.venv/bin/ruff check . && .venv/bin/ruff format .
git add app/web/routes app/web/templates tests/web/test_price_badges.py
git commit -m "Show price-drop and low badges on listings; median column on searches"
```

---

### Task 6: Docs + full verification

**Files:**
- Modify: `docs/SYSTEM_CONTEXT.md`
- Test: full suite + shellcheck-style syntax checks (no new tests)

- [ ] **Step 1: Update SYSTEM_CONTEXT.md**

- §5 fetch-schedule table: add row `| 07:00 | oemparts digest | Email the morning deal digest (price drops + notable new listings) |`.
- §15 "Next Up"/Backlog: strike "Email alerts via AWS SES" phrasing into "done in local-first form (Gmail SMTP digest, 2026-08-24); SES swap deferred to EC2 phase".
- Changelog: new row dated with the completion date summarizing: price_stats module (drop + low-in-search signals, thresholds), digest module + `oemparts digest` + 07:00 launchd job, alerts table now in use (dedup semantics, retry-on-failed-send), dashboard badges + Median column, config additions, test count.

- [ ] **Step 2: Full verification**

```bash
.venv/bin/pytest
.venv/bin/mypy app/core/price_stats.py app/core/digest.py
bash -n scripts/scheduled_job.sh scripts/install_schedules.sh scripts/uninstall_schedules.sh
.venv/bin/ruff check . && .venv/bin/ruff format .
```

- [ ] **Step 3: Commit**

```bash
git add docs/SYSTEM_CONTEXT.md
git commit -m "Update SYSTEM_CONTEXT: price intelligence + morning digest"
```

---

### Task 7: Live validation (manual, with owner)

**Files:** none (operational; findings may spawn fix commits).

- [ ] **Step 1:** Owner creates a Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords) and fills `.env`: `ALERTS_ENABLED=true`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO`. Restart nothing — the CLI reads `.env` per run.
- [ ] **Step 2:** Run `.venv/bin/python oemparts digest` manually. Verify: summary printed; email arrives at dennfish@gmail.com; content reads well on the phone; links open the right listings; alerts stamped (`SELECT count(*) FROM alerts WHERE notified_at IS NOT NULL;`).
- [ ] **Step 3:** Run it again immediately — expect "alerts created: 0, emails sent: 0" (dedup + nothing pending).
- [ ] **Step 4:** Check the dashboard against real data: badges on the listings page (filter by a search), Median $ column on searches; sanity-check the numbers against known prices.
- [ ] **Step 5:** Overnight soak: leave the 07:00 schedule active; confirm next morning's email (or correct silence). If eBay data produces odd content, fix against reality and re-run the affected tests.
- [ ] **Step 6:** Push and open the PR after owner sign-off.
