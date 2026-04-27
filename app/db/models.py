"""SQLAlchemy models for all tables defined in SYSTEM_CONTEXT.md Section 7.

Conventions:
  - UUID primary keys everywhere
  - All timestamps are UTC-aware (TIMESTAMPTZ)
  - Money columns use Numeric(10, 2) — never float
  - User-scoped tables have user_id FK with ON DELETE CASCADE
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Core Tables
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    auth_provider_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, comment="External Clerk/Auth0 ID"
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    subscription_tier: Mapped[str] = mapped_column(
        String(20), default="free", nullable=False, comment="free | pro"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False, comment="active | disabled"
    )

    # Relationships
    vehicles: Mapped[list["Vehicle"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    searches: Mapped[list["Search"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    alerts: Mapped[list["Alert"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    make: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    trim: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vin: Mapped[str | None] = mapped_column(String(17), nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="vehicles")
    searches: Mapped[list["Search"]] = relationship(
        back_populates="vehicle", cascade="all, delete-orphan"
    )


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vehicles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    oem_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    max_price: Mapped[None] = mapped_column(
        Numeric(10, 2), nullable=True, comment="Maximum price filter (Decimal)"
    )
    condition_filter: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="New, Used, or NULL for any"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_high_priority: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="searches")
    vehicle: Mapped["Vehicle"] = relationship(back_populates="searches")
    search_listings: Mapped[list["SearchListing"]] = relationship(
        back_populates="search", cascade="all, delete-orphan"
    )
    alerts: Mapped[list["Alert"]] = relationship(
        back_populates="search", cascade="all, delete-orphan"
    )


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    ebay_item_id: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[None] = mapped_column(
        Numeric(10, 2), nullable=False, comment="Current price (Decimal)"
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    condition: Mapped[str | None] = mapped_column(String(50), nullable=True)
    seller_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    seller_feedback_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seller_feedback_pct: Mapped[None] = mapped_column(
        Numeric(5, 2), nullable=True, comment="Seller positive feedback percentage"
    )
    item_url: Mapped[str] = mapped_column(Text, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ebay_end_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    category_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    compatibility_checked: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Relationships
    search_listings: Mapped[list["SearchListing"]] = relationship(
        back_populates="listing"
    )
    price_history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="listing", cascade="all, delete-orphan"
    )


class SearchListing(Base):
    __tablename__ = "search_listings"
    __table_args__ = (
        UniqueConstraint("search_id", "listing_id", name="uq_search_listing"),
    )

    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("searches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("listings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Relationships
    search: Mapped["Search"] = relationship(back_populates="search_listings")
    listing: Mapped["Listing"] = relationship(back_populates="search_listings")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("listings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price: Mapped[None] = mapped_column(
        Numeric(10, 2), nullable=False, comment="Snapshot price (Decimal)"
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # Relationships
    listing: Mapped["Listing"] = relationship(back_populates="price_history")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    search_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("searches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("listings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alert_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="price_drop | new_listing"
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    channel: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False, comment="email | none"
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="alerts")
    search: Mapped["Search"] = relationship(back_populates="alerts")


# ---------------------------------------------------------------------------
# Cache Tables (shared across users)
# ---------------------------------------------------------------------------


class TaxonomyCache(Base):
    __tablename__ = "taxonomy_cache"
    __table_args__ = (
        UniqueConstraint(
            "category_id",
            "marketplace",
            "year",
            "make",
            "model",
            name="uq_taxonomy_entry",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    category_id: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    marketplace: Mapped[str] = mapped_column(String(20), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    make: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trim: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class VinDecodeCache(Base):
    __tablename__ = "vin_decode_cache"

    vin: Mapped[str] = mapped_column(String(17), primary_key=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    make: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    trim: Mapped[str | None] = mapped_column(String(100), nullable=True)
    body_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    decoded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Operational Tables
# ---------------------------------------------------------------------------


class FetchRun(Base):
    __tablename__ = "fetch_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cycle_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="nightly | intraday | manual"
    )
    searches_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    listings_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    listings_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    listings_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    api_calls_made: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        default="running",
        nullable=False,
        comment="running | completed | failed",
    )


class ApiQuotaLog(Base):
    __tablename__ = "api_quota_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Nullable for system-level calls",
    )
    provider: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="ebay_browse | ebay_taxonomy | nhtsa"
    )
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_uuid
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False, comment="ebay")
    access_token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Stored as plaintext in Phase 1; encrypt in Phase 2+",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
