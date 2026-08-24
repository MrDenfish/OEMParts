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

    def test_genuine_without_number_counted_not_alerted(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        # "Genuine" word alone (no OEM number in title) should not trigger alert
        # even though search has oem_number set.
        seed_baseline(db_session, test_search)
        genuine_no_number = make_listing(
            db_session, "125.00", title="Genuine hose assembly", days_old=0.1
        )
        link(db_session, test_search, genuine_no_number)
        created, other = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 0
        assert other[test_search.id] == 1

    def test_drop_visible_across_real_fetch_cadence(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        """Regression: nightly fetch 03:00, digest 07:00 -> ~28h gap.

        A lookback_days=1 (24h) scan window would miss yesterday's snapshot
        entirely (it's ~28h old), leaving recent_drop with only 1 row in
        the window and no drop detected. SCAN_LOOKBACK_DAYS=2 must span it.
        """
        seed_baseline(db_session, test_search)
        dropped = make_listing(db_session, "48.00", days_old=10)
        link(db_session, test_search, dropped)
        # ~28h ago (yesterday's 03:00 fetch) and ~2.4h ago (today's digest run).
        snapshot(db_session, dropped, "77.71", days_ago=1.2)
        snapshot(db_session, dropped, "48.00", days_ago=0.1)

        created, _ = digest.scan_and_record(
            db_session, test_user.id, utcnow() - timedelta(days=1)
        )
        assert created == 1
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        assert alerts[0].alert_type == "price_drop"
        assert alerts[0].listing_id == dropped.id

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
        assert "cheapest in this search" not in text  # no new items in this fixture
        assert "https://www.ebay.com/itm/1" in html

    def test_render_new_listing_low_copy(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        # New listing that is low-priced should show "among the cheapest" copy.
        seed_baseline(db_session, test_search)
        cheap_new = make_listing(db_session, "50.00", title="Cheap part", days_old=0.1)
        link(db_session, test_search, cheap_new)
        digest.scan_and_record(db_session, test_user.id, utcnow() - timedelta(days=1))
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        subject, text, html = digest.render_digest(db_session, alerts, {})
        assert "among the cheapest in this search" in text
        assert "matches your OEM number" not in text

    def test_render_new_listing_oem_copy(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        # New listing that matches OEM number should show "matches your OEM" copy.
        seed_baseline(db_session, test_search)
        oem_new = make_listing(
            db_session, "500.00", title="Hose LR010819 genuine", days_old=0.1
        )
        link(db_session, test_search, oem_new)
        digest.scan_and_record(db_session, test_user.id, utcnow() - timedelta(days=1))
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        subject, text, html = digest.render_digest(db_session, alerts, {})
        assert "matches your OEM number" in text
        assert "among the cheapest in this search" not in text

    def test_render_new_listing_both_conditions(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        # New listing that is both low-priced AND matches OEM should show both.
        seed_baseline(db_session, test_search)
        cheap_oem = make_listing(
            db_session, "50.00", title="Cheap LR010819 part", days_old=0.1
        )
        link(db_session, test_search, cheap_oem)
        digest.scan_and_record(db_session, test_user.id, utcnow() - timedelta(days=1))
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        subject, text, html = digest.render_digest(db_session, alerts, {})
        assert "among the cheapest in this search; matches your OEM number" in text

    def test_render_escapes_html(
        self, db_session: Session, test_user: User, test_search: Search
    ) -> None:
        seed_baseline(db_session, test_search)
        nasty = make_listing(
            db_session, "50.00", title='<b>A&B "hose"</b>', days_old=0.1
        )
        link(db_session, test_search, nasty)
        digest.scan_and_record(db_session, test_user.id, utcnow() - timedelta(days=1))
        alerts = queries.get_unnotified_alerts(db_session, test_user.id)
        subject, text, html_part = digest.render_digest(db_session, alerts, {})
        assert "&lt;b&gt;" in html_part
        assert "&amp;B" in html_part
        assert "&quot;hose&quot;" in html_part
        assert "<b>A&B" not in html_part
        # Plain-text branch is unaffected — the raw title should appear as-is.
        assert '<b>A&B "hose"</b>' in text

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
        # Seed data that WOULD produce a price-drop alert if scanned, to
        # prove disabled mode never calls scan_and_record (spec: disabled
        # mode computes/prints only, it does not record).
        seed_baseline(db_session, test_search)
        dropped = make_listing(db_session, "48.00", title="Coil pack", days_old=10)
        link(db_session, test_search, dropped)
        snapshot(db_session, dropped, "77.71", days_ago=0.9)
        snapshot(db_session, dropped, "48.00", days_ago=0.05)

        sent = MagicMock(return_value=True)
        monkeypatch.setattr(digest, "send_email", sent)
        summary = digest.run_digest(db_session)
        assert summary.skipped_reason == "alerts_disabled"
        assert summary.alerts_created == 0
        assert sent.call_count == 0
        assert queries.get_unnotified_alerts(db_session, test_user.id) == []


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
