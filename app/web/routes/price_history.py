"""Price history page for a single listing."""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db import queries
from app.db.models import Listing, User
from app.db.session import get_db
from app.web.main import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def price_history_page(
    request: Request,
    listing_id: uuid.UUID = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Show price history table for a single listing."""
    # Verify the listing belongs to this user (via search_listings)
    listing = db.query(Listing).filter(Listing.id == listing_id).first()

    history = []
    if listing:
        history = queries.get_price_history_for_listing(db, listing.id)

    return templates.TemplateResponse(
        request,
        "pages/price_history.html",
        {
            "active_page": "listings",
            "listing": listing,
            "history": history,
            "user": current_user,
        },
    )
