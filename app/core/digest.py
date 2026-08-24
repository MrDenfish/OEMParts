"""Morning digest: scan for deals, record alerts, email the owner.

Flow (spec §4.3-4.4): scan_and_record finds price drops (2-day scan window)
and notable new listings per active search and writes deduped Alert rows
with notified_at NULL. run_digest then emails ALL of a user's unnotified
alerts (so a failed send retries next run) and stamps notified_at on
success. Plain new arrivals are counted in the email but never alerted —
noise control. No email when there is nothing to say.
"""

import html
import logging
import smtplib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.config import settings
from app.core.ai_relevance import classify_listings
from app.core.oem_filter import title_contains_part_number
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
AI_CLASSIFY_CAP = 100

# Snapshots are recorded once per fetch, not continuously. The nightly fetch
# runs at 03:00 and the digest at 07:00, but a Mac asleep at 3 fetches on
# wake — and even on schedule, "yesterday's fetch" can be recorded up to
# ~28 hours before "today's digest run" (03:00 -> 07:00 next day). A 1-day
# (24h) scan window would miss that snapshot entirely, so recent_drop would
# never see the 2 rows it needs to compare. 2 days comfortably covers the
# real cadence; the 7-day alert dedup (ALERT_DEDUP_DAYS) still guarantees a
# given drop is only ever announced once.
SCAN_LOOKBACK_DAYS = 2


@dataclass
class DigestSummary:
    alerts_created: int
    emails_sent: int
    skipped_reason: str | None = None
    classified: int = 0


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
        relevance_map = queries.get_relevance_map(db, search.id)
        listings = queries.get_listings_for_search(db, search.id, active_only=True)

        for listing in listings:
            # Signal 1: price drop within the digest window (own history).
            drop = recent_drop(db, listing.id, lookback_days=SCAN_LOOKBACK_DAYS)
            if drop is not None and not queries.recent_alert_exists(
                db, search.id, listing.id, "price_drop", ALERT_DEDUP_DAYS
            ):
                queries.create_alert(db, user_id, search.id, listing.id, "price_drop")
                created += 1

            # Signal 2: new listing in the window — notable or just counted.
            if listing.first_seen_at >= window_start:
                price = listing.price
                assert isinstance(price, Decimal), "Listing.price must be Decimal"
                verdict = relevance_map.get(listing.id)
                notable = verdict not in ("accessory", "unrelated") and (
                    is_low_in_search(price, stats)
                    or (
                        search.oem_number is not None
                        and title_contains_part_number(listing.title, search.oem_number)
                    )
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


def classify_pending(db: Session, user_id: uuid.UUID) -> int:
    """AI-classify unclassified (search, listing) links, one call per search.

    Fail-open: disabled flag, missing key, or a failed call leaves links
    NULL — tomorrow retries. Returns the number of verdicts persisted.
    """
    if not settings.ai_filter_enabled or not settings.anthropic_api_key:
        return 0

    classified = 0
    for search in queries.get_searches_for_user(db, user_id):
        if not search.is_active:
            continue
        links = queries.get_unclassified_links(db, search.id, AI_CLASSIFY_CAP)
        if not links:
            continue
        if len(links) == AI_CLASSIFY_CAP:
            logger.info(
                "Classification cap (%d) hit for search %s; remainder tomorrow",
                AI_CLASSIFY_CAP,
                search.id,
            )
        listings = [
            listing
            for link in links
            if (listing := db.get(Listing, link.listing_id)) is not None
        ]
        verdicts = classify_listings(search, listings)
        if verdicts is None:
            continue
        for listing_id, verdict in verdicts.items():
            queries.set_link_relevance(db, search.id, listing_id, verdict)
            classified += 1
        db.commit()
    return classified


def _fmt(price: Decimal) -> str:
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
        # Compute stats once per search for new_listing reason determination.
        stats = search_price_stats(db, search_id)

        text_lines.append(f"\n== {search.query_text} ==")
        html_lines.append(f"<h3>{html.escape(search.query_text)}</h3><ul>")
        for alert in search_alerts[:DETAIL_CAP_PER_SEARCH]:
            listing = db.get(Listing, alert.listing_id)
            if listing is None:
                continue
            if alert.alert_type == "price_drop":
                drop = recent_drop(db, listing.id, lookback_days=7)
                price = listing.price
                assert isinstance(price, Decimal), "Listing.price must be Decimal"
                detail = (
                    f"{_fmt(drop.old_price)} -> {_fmt(drop.new_price)} (-{drop.pct}%)"
                    if drop
                    else _fmt(price)
                )
                label = "PRICE DROP"
            else:
                # Determine the actual reason this new listing is notable.
                price = listing.price
                assert isinstance(price, Decimal), "Listing.price must be Decimal"
                low = is_low_in_search(price, stats)
                oem = search.oem_number is not None and title_contains_part_number(
                    listing.title, search.oem_number
                )

                if low and oem:
                    reason = (
                        "among the cheapest in this search; matches your OEM number"
                    )
                elif low:
                    reason = "among the cheapest in this search"
                elif oem:
                    reason = "matches your OEM number"
                else:
                    reason = "new"

                detail = f"{_fmt(price)} — {reason}"
                label = "NEW"
            text_lines.append(f"[{label}] {listing.title[:70]} — {detail}")
            text_lines.append(f"    {listing.item_url}")
            safe_title = html.escape(listing.title[:70])
            safe_url = html.escape(listing.item_url, quote=True)
            safe_detail = html.escape(detail)
            html_lines.append(
                f'<li><b>{label}</b>: <a href="{safe_url}">'
                f"{safe_title}</a> — {safe_detail}</li>"
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
    total_classified = 0
    emails_sent = 0

    if not settings.alerts_enabled:
        return DigestSummary(
            alerts_created=0, emails_sent=0, skipped_reason="alerts_disabled"
        )

    users = db.query(User).all()
    for user in users:
        total_classified += classify_pending(db, user.id)
        created, other_new = scan_and_record(db, user.id, window_start)
        total_created += created

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

    return DigestSummary(
        alerts_created=total_created,
        emails_sent=emails_sent,
        skipped_reason=None,
        classified=total_classified,
    )
