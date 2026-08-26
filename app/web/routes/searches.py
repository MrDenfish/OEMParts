"""Parts watchlist management routes."""

import logging
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.config import settings
from app.core.ai_relevance import craft_query, looks_like_part_number
from app.core.compatibility import resolve_fitment_category
from app.core.price_stats import search_price_stats
from app.core.search_runner import run_single_search
from app.db import queries
from app.db.models import Search, User
from app.db.session import get_db

# `templates` is intentionally NOT imported at module level here: app.web.main
# imports this module (to register `router`) as part of building the app, so
# a module-level `from app.web.main import templates` creates a circular
# import that breaks if anything ever imports app.web.routes.searches
# directly, before app.web.main has been loaded (some tests do this). Each
# handler that needs it imports it locally instead — see conftest.py's
# `client` fixture for the same pattern.

logger = logging.getLogger(__name__)

router = APIRouter()


def _row_context(db: Session, search: Search) -> dict:
    """Context for components/search_row.html — keeps HTMX row re-renders
    consistent with the full-page render (median column)."""
    return {"search": search, "price_stats": search_price_stats(db, search.id)}


@router.get("/", response_class=HTMLResponse)
def searches_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full page listing all searches, grouped by vehicle."""
    from app.web.main import templates

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
            "search_stats": {s.id: search_price_stats(db, s.id) for s in user_searches},
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
    from app.web.main import templates

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

    parsed_oem_number = oem_number.strip() if oem_number else None
    if parsed_oem_number == "":
        parsed_oem_number = None

    final_query = query_text.strip()

    # Auto-copy (owner-approved UX, 2026-08-26 incident): if the OEM field
    # is empty but the query text is itself the shape of a bare part
    # number (e.g. "LR072537"), treat it as the OEM number too — typing
    # only a part number as the search clearly means "this is the part
    # number I want". Must run before default_oem_only is computed so the
    # existing OEM-present default applies to the auto-set number exactly
    # as if the user had typed it into the OEM field.
    if parsed_oem_number is None and looks_like_part_number(final_query):
        parsed_oem_number = final_query.upper()

    # Default ON when an OEM number is provided. The user wanted to
    # default OEM-only filtering on for searches that have a part number,
    # since that's the case where it most reliably trims aftermarket noise.
    default_oem_only = parsed_oem_number is not None

    # AI query crafting (spec §4.5): refine the query text before category
    # resolution so both benefit. Best-effort — any failure keeps the
    # user's text verbatim. Skipped for bare part numbers: the AI cannot
    # know what a bare number is, and guessing caused the 2026-08-26
    # incident (LR072537 guessed as "water pump", actually a compressor).
    if (
        settings.ai_filter_enabled
        and settings.anthropic_api_key
        and not looks_like_part_number(final_query)
    ):
        vehicle = queries.get_vehicle_by_id(db, vehicle_id, current_user.id)
        if vehicle is not None:
            # category_name is None by design: crafting runs before
            # category resolution below, so there's no category yet.
            crafted = craft_query(final_query, parsed_oem_number, vehicle, None)
            if crafted:
                final_query = crafted

    # Resolve an eBay fitment category from the query text. Inline eBay
    # call — sanctioned by the taxonomy exception in CLAUDE.md. Failure
    # must never block search creation; NULL just means fallback mode.
    category_id: str | None = None
    category_name: str | None = None
    try:
        resolved = resolve_fitment_category(db, final_query)
        if resolved is not None:
            category_id = resolved.category_id
            category_name = resolved.category_name
    except Exception:
        logger.exception("Category resolution errored for query '%s'", final_query)

    search = queries.create_search(
        db,
        user_id=current_user.id,
        vehicle_id=vehicle_id,
        query_text=final_query,
        oem_number=parsed_oem_number,
        max_price=parsed_price,
        condition_filter=parsed_condition,
        is_high_priority=is_high_priority,
        oem_only=default_oem_only,
        category_id=category_id,
        category_name=category_name,
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
            _row_context(db, search),
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
    from app.web.main import templates

    search = queries.toggle_search_active(db, search_id, current_user.id)
    db.commit()

    if search is None:
        return Response(status_code=404, content="Search not found")

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/search_row.html",
            _row_context(db, search),
        )

    return RedirectResponse(url="/searches", status_code=303)


@router.patch("/{search_id}/toggle-oem-only", response_model=None)
def toggle_search_oem_only(
    request: Request,
    search_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle a search's OEM-only title filter."""
    from app.web.main import templates

    search = queries.toggle_search_oem_only(db, search_id, current_user.id)
    db.commit()

    if search is None:
        return Response(status_code=404, content="Search not found")

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/search_row.html",
            _row_context(db, search),
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
    from app.web.main import templates

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
            _row_context(db, search),
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
