"""Landing / overview page route."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db import queries
from app.db.models import User
from app.db.session import get_db
from app.web.main import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Dashboard overview: vehicle count, search count, latest fetch info."""
    vehicles = queries.get_vehicles_for_user(db, current_user.id)
    user_searches = queries.get_searches_for_user(db, current_user.id)
    listing_count = queries.count_listings_for_user(db, current_user.id)
    latest_fetch = queries.get_latest_fetch_run(db)

    return templates.TemplateResponse(
        request,
        "pages/home.html",
        {
            "active_page": "home",
            "user": current_user,
            "vehicle_count": len(vehicles),
            "search_count": len(user_searches),
            "active_search_count": sum(1 for s in user_searches if s.is_active),
            "listing_count": listing_count,
            "latest_fetch": latest_fetch,
        },
    )
