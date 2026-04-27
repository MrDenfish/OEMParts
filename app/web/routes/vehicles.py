"""My vehicles (add/edit/delete) routes."""

import uuid

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db import queries
from app.db.models import User
from app.db.session import get_db
from app.web.main import templates

router = APIRouter()


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
        },
    )


@router.post("/", response_model=None)
def create_vehicle(
    request: Request,
    year: int = Form(...),
    make: str = Form(...),
    model: str = Form(...),
    trim: str | None = Form(None),
    nickname: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new vehicle. Returns partial row for HTMX or redirects."""
    vehicle = queries.create_vehicle(
        db,
        user_id=current_user.id,
        year=year,
        make=make.strip(),
        model=model.strip(),
        trim=trim.strip() if trim else None,
        nickname=nickname.strip() if nickname else None,
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
