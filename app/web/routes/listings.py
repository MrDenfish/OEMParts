"""Listing browser and filter routes."""

import uuid
from decimal import Decimal, InvalidOperation
from typing import cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.price_stats import is_low_in_search, recent_drop, search_price_stats
from app.db import queries
from app.db.models import User
from app.db.session import get_db
from app.web.main import templates

router = APIRouter()

LISTINGS_PER_PAGE = 50


@router.get("/", response_class=HTMLResponse)
def listings_page(
    request: Request,
    search_id: uuid.UUID | None = Query(None),
    vehicle_id: uuid.UUID | None = Query(None),
    min_price: str | None = Query(None),
    max_price: str | None = Query(None),
    condition: str | None = Query(None),
    active_only: bool = Query(True),
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Browse listings with optional filters."""
    # Parse price filters safely
    parsed_min: Decimal | None = None
    parsed_max: Decimal | None = None
    if min_price and min_price.strip():
        try:
            parsed_min = Decimal(min_price.strip())
        except InvalidOperation:
            pass
    if max_price and max_price.strip():
        try:
            parsed_max = Decimal(max_price.strip())
        except InvalidOperation:
            pass

    offset = (page - 1) * LISTINGS_PER_PAGE

    listing_list = queries.get_listings_for_user(
        db,
        user_id=current_user.id,
        search_id=search_id,
        vehicle_id=vehicle_id,
        min_price=parsed_min,
        max_price=parsed_max,
        condition=condition,
        active_only=active_only,
        limit=LISTINGS_PER_PAGE,
        offset=offset,
    )

    # Get vehicles and searches for filter dropdowns
    vehicles = queries.get_vehicles_for_user(db, current_user.id)
    user_searches = queries.get_searches_for_user(db, current_user.id)

    # Deal badges (spec §4.2): drop = listing vs its own 7-day history;
    # low = cheapest quartile of the filtered search (only meaningful when
    # a single search is selected).
    #
    # Multi-tenancy contract (CLAUDE.md): search_id is a raw query param, so
    # confirm the search belongs to current_user before computing stats from
    # it — otherwise a user could probe another user's search by id and infer
    # its price distribution via the "low" badge.
    stats = None
    owned_search = None
    if search_id is not None:
        owned_search = queries.get_search_by_id(db, search_id, current_user.id)
        if owned_search is not None:
            stats = search_price_stats(db, search_id)

    # AI relevance tags (Task 5): fetch the relevance map for the owned search
    relevance_map = (
        queries.get_relevance_map(db, owned_search.id) if owned_search else {}
    )

    # cast: Listing.price is a Numeric(10, 2) column, always a Decimal at
    # runtime, but the model's `Mapped[None]` annotation (pre-existing typo,
    # out of scope here — see app/db/models.py) makes mypy infer None.
    listing_extras = {
        listing.id: {
            "drop": recent_drop(db, listing.id, lookback_days=7),
            "low": is_low_in_search(cast(Decimal, listing.price), stats),
            "relevance": relevance_map.get(listing.id),
        }
        for listing in listing_list
    }

    template_name = "pages/listings.html"

    # HTMX request: return just the table body
    if request.headers.get("HX-Request"):
        template_name = "components/listing_table.html"

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "active_page": "listings",
            "listings": listing_list,
            "listing_extras": listing_extras,
            "vehicles": vehicles,
            "searches": user_searches,
            "user": current_user,
            # Pass current filter values back for form state
            "filter_search_id": str(search_id) if search_id else "",
            "filter_vehicle_id": str(vehicle_id) if vehicle_id else "",
            "filter_min_price": min_price or "",
            "filter_max_price": max_price or "",
            "filter_condition": condition or "",
            "filter_active_only": active_only,
            "page": page,
            "has_more": len(listing_list) == LISTINGS_PER_PAGE,
        },
    )
