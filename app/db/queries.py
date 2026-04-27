"""Query helpers for common database operations.

All user-scoped queries filter by user_id to enforce multi-tenancy.
Each function takes a Session as its first argument.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from app.db.models import (
    FetchRun,
    Listing,
    PriceHistory,
    Search,
    SearchListing,
    User,
    Vehicle,
    utcnow,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User queries
# ---------------------------------------------------------------------------


def get_user_by_email(db: Session, email: str) -> User | None:
    """Look up a user by email."""
    return db.query(User).filter(User.email == email).first()


def create_user(db: Session, email: str) -> User:
    """Create a new user with the given email."""
    user = User(email=email)
    db.add(user)
    db.flush()
    return user


# ---------------------------------------------------------------------------
# Vehicle queries (user-scoped)
# ---------------------------------------------------------------------------


def get_vehicles_for_user(db: Session, user_id: uuid.UUID) -> list[Vehicle]:
    """Return all vehicles for a user, ordered by creation date."""
    return (
        db.query(Vehicle)
        .filter(Vehicle.user_id == user_id)
        .order_by(Vehicle.created_at.desc())
        .all()
    )


def get_vehicle_by_id(
    db: Session, vehicle_id: uuid.UUID, user_id: uuid.UUID
) -> Vehicle | None:
    """Return a single vehicle, scoped to the user."""
    return (
        db.query(Vehicle)
        .filter(Vehicle.id == vehicle_id, Vehicle.user_id == user_id)
        .first()
    )


def create_vehicle(
    db: Session,
    user_id: uuid.UUID,
    year: int,
    make: str,
    model: str,
    trim: str | None = None,
    nickname: str | None = None,
) -> Vehicle:
    """Create a new vehicle for a user."""
    vehicle = Vehicle(
        user_id=user_id,
        year=year,
        make=make,
        model=model,
        trim=trim,
        nickname=nickname,
    )
    db.add(vehicle)
    db.flush()
    return vehicle


def delete_vehicle(db: Session, vehicle_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Delete a vehicle (cascades to searches). Returns True if found and deleted."""
    vehicle = get_vehicle_by_id(db, vehicle_id, user_id)
    if vehicle is None:
        return False
    db.delete(vehicle)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# Search queries (user-scoped)
# ---------------------------------------------------------------------------


def get_searches_for_user(db: Session, user_id: uuid.UUID) -> list[Search]:
    """Return all searches for a user, ordered by creation date."""
    return (
        db.query(Search)
        .filter(Search.user_id == user_id)
        .order_by(Search.created_at.desc())
        .all()
    )


def get_active_searches(db: Session, cycle_type: str = "nightly") -> list[Search]:
    """Return searches to process for a fetch cycle.

    nightly  = all active searches (across all users)
    intraday = only high-priority active searches
    manual   = not used here (caller passes specific search_id)
    """
    query = db.query(Search).filter(Search.is_active.is_(True))
    if cycle_type == "intraday":
        query = query.filter(Search.is_high_priority.is_(True))
    return query.all()


def get_search_by_id(
    db: Session, search_id: uuid.UUID, user_id: uuid.UUID
) -> Search | None:
    """Return a single search, scoped to the user."""
    return (
        db.query(Search)
        .filter(Search.id == search_id, Search.user_id == user_id)
        .first()
    )


def get_search_by_id_unscoped(db: Session, search_id: uuid.UUID) -> Search | None:
    """Return a search without user filter.

    Used only by CLI/worker where the caller has shell access and is trusted.
    Do NOT use in web request handlers.
    """
    return db.query(Search).filter(Search.id == search_id).first()


def create_search(
    db: Session,
    user_id: uuid.UUID,
    vehicle_id: uuid.UUID,
    query_text: str,
    oem_number: str | None = None,
    max_price: Decimal | None = None,
    condition_filter: str | None = None,
    is_high_priority: bool = False,
) -> Search:
    """Create a new search for a user."""
    search = Search(
        user_id=user_id,
        vehicle_id=vehicle_id,
        query_text=query_text,
        oem_number=oem_number,
        max_price=max_price,
        condition_filter=condition_filter,
        is_high_priority=is_high_priority,
    )
    db.add(search)
    db.flush()
    return search


def toggle_search_active(
    db: Session, search_id: uuid.UUID, user_id: uuid.UUID
) -> Search | None:
    """Toggle a search's is_active flag. Returns the updated search or None."""
    search = get_search_by_id(db, search_id, user_id)
    if search is None:
        return None
    search.is_active = not search.is_active
    db.flush()
    return search


def delete_search(db: Session, search_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Delete a search. Returns True if found and deleted."""
    search = get_search_by_id(db, search_id, user_id)
    if search is None:
        return False
    db.delete(search)
    db.flush()
    return True


def update_search_last_fetched(db: Session, search_id: uuid.UUID) -> None:
    """Mark a search as just fetched."""
    db.execute(
        update(Search).where(Search.id == search_id).values(last_fetched_at=utcnow())
    )
    db.flush()


# ---------------------------------------------------------------------------
# Listing queries
# ---------------------------------------------------------------------------


def get_listing_by_ebay_id(db: Session, ebay_item_id: str) -> Listing | None:
    """Look up a listing by its eBay item ID."""
    return db.query(Listing).filter(Listing.ebay_item_id == ebay_item_id).first()


def upsert_listing(
    db: Session,
    ebay_item_id: str,
    title: str,
    price: Decimal,
    currency: str,
    item_url: str,
    condition: str | None = None,
    seller_name: str | None = None,
    seller_feedback_score: int | None = None,
    seller_feedback_pct: Decimal | None = None,
    image_url: str | None = None,
    ebay_end_date: datetime | None = None,
    category_id: str | None = None,
) -> tuple[Listing, bool]:
    """Insert or update a listing by ebay_item_id.

    Returns (listing, is_new). If the listing already exists, updates
    price, last_seen_at, and other mutable fields.
    """
    existing = get_listing_by_ebay_id(db, ebay_item_id)
    if existing is not None:
        # Update mutable fields
        existing.price = price
        existing.title = title
        existing.condition = condition
        existing.seller_name = seller_name
        existing.seller_feedback_score = seller_feedback_score
        existing.seller_feedback_pct = seller_feedback_pct
        existing.item_url = item_url
        existing.image_url = image_url
        existing.ebay_end_date = ebay_end_date
        existing.last_seen_at = utcnow()
        existing.is_active = True
        db.flush()
        return existing, False

    listing = Listing(
        ebay_item_id=ebay_item_id,
        title=title,
        price=price,
        currency=currency,
        condition=condition,
        seller_name=seller_name,
        seller_feedback_score=seller_feedback_score,
        seller_feedback_pct=seller_feedback_pct,
        item_url=item_url,
        image_url=image_url,
        ebay_end_date=ebay_end_date,
        category_id=category_id,
        is_active=True,
    )
    db.add(listing)
    db.flush()
    return listing, True


def get_listings_for_search(
    db: Session,
    search_id: uuid.UUID,
    active_only: bool = True,
) -> list[Listing]:
    """Return listings linked to a specific search."""
    query = (
        db.query(Listing)
        .join(SearchListing, SearchListing.listing_id == Listing.id)
        .filter(SearchListing.search_id == search_id)
    )
    if active_only:
        query = query.filter(Listing.is_active.is_(True))
    return query.order_by(Listing.last_seen_at.desc()).all()


def get_listings_for_user(
    db: Session,
    user_id: uuid.UUID,
    search_id: uuid.UUID | None = None,
    vehicle_id: uuid.UUID | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    condition: str | None = None,
    active_only: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[Listing]:
    """Return listings for a user with optional filters.

    Joins through search_listings -> searches to enforce user_id scope.
    """
    query = (
        db.query(Listing)
        .join(SearchListing, SearchListing.listing_id == Listing.id)
        .join(Search, Search.id == SearchListing.search_id)
        .filter(Search.user_id == user_id)
    )

    if search_id is not None:
        query = query.filter(SearchListing.search_id == search_id)
    if vehicle_id is not None:
        query = query.filter(Search.vehicle_id == vehicle_id)
    if min_price is not None:
        query = query.filter(Listing.price >= min_price)
    if max_price is not None:
        query = query.filter(Listing.price <= max_price)
    if condition is not None:
        query = query.filter(Listing.condition == condition)
    if active_only:
        query = query.filter(Listing.is_active.is_(True))

    return (
        query.distinct()
        .order_by(Listing.last_seen_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def count_listings_for_user(
    db: Session,
    user_id: uuid.UUID,
    active_only: bool = True,
) -> int:
    """Count total listings across all of a user's searches."""
    query = (
        db.query(Listing.id)
        .join(SearchListing, SearchListing.listing_id == Listing.id)
        .join(Search, Search.id == SearchListing.search_id)
        .filter(Search.user_id == user_id)
    )
    if active_only:
        query = query.filter(Listing.is_active.is_(True))
    return query.distinct().count()


# ---------------------------------------------------------------------------
# SearchListing queries
# ---------------------------------------------------------------------------


def link_search_to_listing(
    db: Session, search_id: uuid.UUID, listing_id: uuid.UUID
) -> None:
    """Link a search to a listing (idempotent)."""
    existing = (
        db.query(SearchListing)
        .filter(
            SearchListing.search_id == search_id,
            SearchListing.listing_id == listing_id,
        )
        .first()
    )
    if existing is None:
        link = SearchListing(search_id=search_id, listing_id=listing_id)
        db.add(link)
        db.flush()


# ---------------------------------------------------------------------------
# PriceHistory queries
# ---------------------------------------------------------------------------


def record_price_snapshot(
    db: Session, listing_id: uuid.UUID, price: Decimal
) -> PriceHistory:
    """Record a price snapshot for a listing."""
    snapshot = PriceHistory(listing_id=listing_id, price=price)
    db.add(snapshot)
    db.flush()
    return snapshot


def get_price_history_for_listing(
    db: Session, listing_id: uuid.UUID
) -> list[PriceHistory]:
    """Return price history for a listing, most recent first."""
    return (
        db.query(PriceHistory)
        .filter(PriceHistory.listing_id == listing_id)
        .order_by(PriceHistory.recorded_at.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# FetchRun queries
# ---------------------------------------------------------------------------


def create_fetch_run(db: Session, cycle_type: str) -> FetchRun:
    """Create a new fetch run record."""
    run = FetchRun(cycle_type=cycle_type)
    db.add(run)
    db.flush()
    return run


def complete_fetch_run(
    db: Session,
    fetch_run: FetchRun,
    searches_processed: int,
    listings_fetched: int,
    listings_new: int,
    listings_updated: int,
    api_calls_made: int,
    errors: int,
) -> None:
    """Mark a fetch run as completed with summary stats."""
    fetch_run.completed_at = utcnow()
    fetch_run.searches_processed = searches_processed
    fetch_run.listings_fetched = listings_fetched
    fetch_run.listings_new = listings_new
    fetch_run.listings_updated = listings_updated
    fetch_run.api_calls_made = api_calls_made
    fetch_run.errors = errors
    fetch_run.status = "completed" if errors < searches_processed else "failed"
    db.flush()


def get_latest_fetch_run(db: Session) -> FetchRun | None:
    """Return the most recent fetch run."""
    return db.query(FetchRun).order_by(FetchRun.started_at.desc()).first()


# ---------------------------------------------------------------------------
# Cleanup queries
# ---------------------------------------------------------------------------


def mark_stale_listings_inactive(db: Session, missing_cycles: int) -> int:
    """Mark listings as inactive if not seen recently.

    A listing is stale if last_seen_at is older than
    (TTL * missing_cycles) ago. Returns count of rows updated.
    """
    from app.config import settings

    ttl_minutes = settings.fetch_default_ttl_minutes
    cutoff = utcnow() - timedelta(minutes=ttl_minutes * missing_cycles)

    result = db.execute(
        update(Listing)
        .where(Listing.is_active.is_(True), Listing.last_seen_at < cutoff)
        .values(is_active=False)
    )
    db.flush()
    return result.rowcount  # type: ignore[return-value]


def archive_old_listings(db: Session, older_than_days: int) -> int:
    """Delete inactive listings older than the given threshold.

    CASCADE will clean up related price_history and search_listings rows.
    Returns count of rows deleted.
    """
    cutoff = utcnow() - timedelta(days=older_than_days)

    result = db.execute(
        delete(Listing).where(
            Listing.is_active.is_(False), Listing.last_seen_at < cutoff
        )
    )
    db.flush()
    return result.rowcount  # type: ignore[return-value]
