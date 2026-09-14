"""My vehicles (add/edit/delete) routes."""

import logging
import uuid

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.compatibility import canonicalize_make, canonicalize_model
from app.db import queries
from app.db.models import User
from app.db.session import get_db
from app.sources.nhtsa_vpic import decode_vin, get_models_for_make_year, is_valid_vin
from app.web.main import templates
from app.web.makes import COMMON_MAKES

logger = logging.getLogger(__name__)

router = APIRouter()

YEARS = tuple(range(2027, 1980, -1))


@router.get("/", response_class=HTMLResponse)
def vehicles_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full page listing all vehicles."""
    vehicles = queries.get_vehicles_for_user(db, current_user.id)
    return templates.TemplateResponse(
        request,
        "pages/vehicles.html",
        {
            "active_page": "vehicles",
            "vehicles": vehicles,
            "user": current_user,
            "makes": COMMON_MAKES,
            "years": YEARS,
            "mode": "dropdown",
        },
    )


@router.post("/", response_model=None)
def create_vehicle(
    request: Request,
    year: int = Form(...),
    make: str = Form(""),
    model: str = Form(""),
    make_text: str = Form(""),
    model_text: str = Form(""),
    vin: str | None = Form(None),
    trim: str | None = Form(None),
    nickname: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a vehicle. Free-text overrides the dropdowns; VIN optional."""
    final_make = make_text.strip() or (make.strip() if make != "__other__" else "")
    final_model = model_text.strip() or (model.strip() if model != "__other__" else "")
    if not final_make or not final_model:
        response = templates.TemplateResponse(
            request,
            "components/vehicle_fields.html",
            {
                "mode": "dropdown",
                "makes": COMMON_MAKES,
                "years": YEARS,
                "error": "Make and model are required — pick or type them.",
            },
        )
        response.headers["HX-Retarget"] = "#vehicle-fields"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    # Canonicalize to eBay's fitment-catalog spelling. The Browse API's
    # compatibility_filter is case-sensitive ("LAND ROVER" silently matched
    # 48 listings where "Land Rover" matched 125 — live incident 2026-09-14),
    # and NHTSA VIN decodes return all-caps makes/models. Inline taxonomy
    # call, sanctioned by the CLAUDE.md exception; cached 30 days. Failure
    # must never block vehicle creation — the raw values are kept.
    try:
        final_make = canonicalize_make(db, final_make)
        final_model = canonicalize_model(db, final_make, final_model)
    except Exception:
        logger.exception(
            "Fitment canonicalization errored for '%s %s'", final_make, final_model
        )

    clean_vin = vin.strip().upper() if vin else None
    vehicle = queries.create_vehicle(
        db,
        user_id=current_user.id,
        year=year,
        make=final_make,
        model=final_model,
        trim=trim.strip() if trim else None,
        nickname=nickname.strip() if nickname else None,
        vin=clean_vin if clean_vin and is_valid_vin(clean_vin) else None,
    )
    db.commit()

    # HTMX request: return just the new row
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "components/vehicle_row.html",
            {"vehicle": vehicle},
        )

    return RedirectResponse(url="/vehicles", status_code=303)


@router.post("/decode-vin", response_class=HTMLResponse)
def decode_vin_endpoint(
    request: Request,
    vin_lookup: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """HTMX: decode a VIN and return the fields partial (never 500s)."""
    decoded = decode_vin(db, vin_lookup)
    if decoded is None or not (decoded.year or decoded.make or decoded.model):
        return templates.TemplateResponse(
            request,
            "components/vehicle_fields.html",
            {
                "mode": "dropdown",
                "makes": COMMON_MAKES,
                "years": YEARS,
                "error": (
                    "Could not decode that VIN — pick or type the details instead."
                ),
            },
        )
    # Show eBay's canonical spelling in the form, not NHTSA's all-caps
    # ("LAND ROVER" → "Land Rover"). The create route canonicalizes again
    # at storage time, so this is display polish — same fail-open rule.
    try:
        if decoded.make:
            decoded.make = canonicalize_make(db, decoded.make)
        if decoded.make and decoded.model:
            decoded.model = canonicalize_model(db, decoded.make, decoded.model)
    except Exception:
        logger.exception(
            "Fitment canonicalization errored for decoded '%s %s'",
            decoded.make,
            decoded.model,
        )

    return templates.TemplateResponse(
        request,
        "components/vehicle_fields.html",
        {
            "mode": "decoded",
            "decoded": decoded,
            "vin": vin_lookup.strip().upper(),
        },
    )


@router.get("/models", response_class=HTMLResponse)
def model_options(
    request: Request,
    make: str = "",
    year: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """HTMX: <option> list for a make+year (fail-soft to the Other option).

    year is a str, not int, because the Make select's hx-get fires with
    year="" before a year is picked — FastAPI's int default only applies
    when the param is absent, not when it's an empty string, and would
    422 on that request.
    """
    year_int = int(year) if year.isdigit() else 0
    models = None
    if make and make != "__other__" and year_int:
        models = get_models_for_make_year(db, make, year_int)
    return templates.TemplateResponse(
        request,
        "components/model_options.html",
        {"models": models or []},
    )


@router.delete("/{vehicle_id}")
def delete_vehicle(
    vehicle_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Delete a vehicle and its searches (CASCADE)."""
    deleted = queries.delete_vehicle(db, vehicle_id, current_user.id)
    db.commit()
    if deleted:
        return Response(status_code=200, content="")
    return Response(status_code=404, content="Vehicle not found")
