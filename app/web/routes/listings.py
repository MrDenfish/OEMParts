"""Listing browser and filter routes."""

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
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
