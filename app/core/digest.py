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
from decimal import Decimal
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
                queries.create_alert(db, user_id, search.id, listing.id, "price_drop")
                created += 1

            # Signal 2: new listing in the window — notable or just counted.
            if listing.first_seen_at >= window_start:
                price = listing.price
                assert isinstance(price, Decimal), "Listing.price must be Decimal"
                notable = is_low_in_search(price, stats) or (
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
        parts.append(f"{len(news)} notable listing{'s' if len(news) != 1 else ''}")
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
                    f"{_fmt(drop.old_price)} -> {_fmt(drop.new_price)} (-{drop.pct}%)"
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
    html_lines.append(
        '<p><a href="http://localhost:8000/listings">Open the dashboard</a></p>'
    )
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
