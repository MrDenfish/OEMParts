"""Shared helpers: formatting utilities for templates."""

from decimal import Decimal
from datetime import datetime


def format_price(price: Decimal | None, currency: str = "USD") -> str:
    """Format a Decimal price for display. Returns '$123.45' or 'N/A'."""
    if price is None:
        return "N/A"
    return f"${price:,.2f}"


def format_datetime(dt: datetime | None) -> str:
    """Format a datetime for display. Returns 'Apr 26, 2026 1:30 PM' or 'Never'."""
    if dt is None:
        return "Never"
    return dt.strftime("%b %d, %Y %I:%M %p")


def format_datetime_short(dt: datetime | None) -> str:
    """Short date format. Returns 'Apr 26' or 'Never'."""
    if dt is None:
        return "Never"
    return dt.strftime("%b %d")


def vehicle_display_name(
    year: int, make: str, model: str, nickname: str | None = None
) -> str:
    """Build a display string for a vehicle."""
    base = f"{year} {make} {model}"
    if nickname:
        return f"{nickname} ({base})"
    return base
