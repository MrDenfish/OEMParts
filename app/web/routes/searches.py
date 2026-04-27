"""Parts watchlist management routes."""

import logging
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.search_runner import run_single_search
from app.db import queries
from app.db.models import User
from app.db.session import get_db
from app.web.main import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def searches_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full page listing all searches, grouped by vehicle."""
    vehicles = queries.get_vehicles_for_user(db, current_user.id)
    user_searches = queries.get_searches_for_user(db, current_user.id)

    # Group searches by vehicle for display
    searches_by_vehicle: dict[uuid.UUID, list] = {}
    for vehicle in vehicles:
        searches_by_vehicle[vehicle.id] = []
    for search in user_searches:
        if search.vehicle_id in searches_by_vehicle:
            searches_by_vehicle[search.vehicle_id].append(search)

    return templates.TemplateResponse(
        request,
        "pages/searches.html",
        {
            "active_page": "searches",
            "vehicles": vehicles,
            "searches_by_vehicle": searches_by_vehicle,
            "user": current_user,
        },
    )


@router.post("/", response_model=None)
def create_search(
    request: Request,
    vehicle_id: uuid.UUID = Form(...),
    query_text: str = Form(...),
    oem_number: str | None = Form(None),
    max_price: str | None = Form(None),
    condition_filter: str | None = Form(None),
    is_high_priority: bool = Form(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new part search."""
    # Parse max_price safely
    parsed_price: Decimal | None = None
    if max_price and max_price.strip():
        try:
            parsed_price = Decimal(max_price.strip())
        except InvalidOperation:
            parsed_price = None

    # Normalize condition_filter (empty string -> None)
    parsed_condition = condition_filter.strip() if condition_filter else None
    if parsed_condition == "":
        parsed_condition = None

    search = queries.create_search(
        db,
        user_id=current_user.id,
        vehicle_id=vehicle_id,
        query_text=query_text.strip(),
        oem_number=oem_number.strip() if oem_number else None,
        max_price=parsed_price,
        condition_filter=parsed_condition,
        is_high_priority=is_high_priority,
    )
    db.commit()
    db.refresh(search)

    # Auto-fetch listings for the new search
    try:
        result = run_single_search(db, search)
        logger.info(
            "Auto-fetch for new search '%s': %d new listings",
            search.query_text,
            result.listings_new,
        )
        db.refresh(search)
    except Exception:
        logger.exception("Auto-fetch failed for new search '%s'", search.query_text)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/search_row.html",
            {"search": search},
        )

    return RedirectResponse(url="/searches", status_code=303)


@router.patch("/{search_id}/toggle", response_model=None)
def toggle_search(
    request: Request,
    search_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle a search between active and inactive."""
    search = queries.toggle_search_active(db, search_id, current_user.id)
    db.commit()

    if search is None:
        return Response(status_code=404, content="Search not found")

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/search_row.html",
            {"search": search},
        )

    return RedirectResponse(url="/searches", status_code=303)


@router.post("/{search_id}/fetch", response_model=None)
def fetch_search(
    request: Request,
    search_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Trigger an immediate fetch for a single search."""
    search = queries.get_search_by_id(db, search_id, current_user.id)
    if search is None:
        return Response(status_code=404, content="Search not found")

    result = run_single_search(db, search)
    logger.info(
        "Manual fetch for '%s': %d new, %d updated",
        search.query_text,
        result.listings_new,
        result.listings_updated,
    )

    # Refresh the search object to pick up updated last_fetched_at
    db.refresh(search)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/search_row.html",
            {"search": search},
        )

    return RedirectResponse(url="/searches", status_code=303)


@router.delete("/{search_id}")
def delete_search(
    search_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete a search."""
    deleted = queries.delete_search(db, search_id, current_user.id)
    db.commit()
    if deleted:
        return Response(status_code=200, content="")
    return Response(status_code=404, content="Search not found")
