"""FastAPI application factory and startup configuration.

Configures Jinja2 templates, static files, session middleware, and route mounting.
Run with: PYTHONPATH=$PWD uvicorn app.web.main:app --reload --port 8000
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.web.routes._helpers import format_datetime, format_price, vehicle_display_name

logger = logging.getLogger(__name__)

# Paths relative to this file
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Shared Jinja2 templates instance — imported by route modules
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Register custom template filters and globals
templates.env.filters["format_price"] = format_price
templates.env.filters["format_datetime"] = format_datetime
templates.env.globals["vehicle_display_name"] = vehicle_display_name


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(
        title="OEMPartsAgent",
        docs_url=None,  # No Swagger UI (server-rendered app)
        redoc_url=None,  # No ReDoc
    )

    # Session middleware for cookie-based auth persistence
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key,
    )

    # Static files (CSS, JS)
    application.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    # Include route modules
    from app.web.routes import home, listings, price_history, searches, vehicles

    application.include_router(home.router)
    application.include_router(vehicles.router, prefix="/vehicles", tags=["vehicles"])
    application.include_router(searches.router, prefix="/searches", tags=["searches"])
    application.include_router(listings.router, prefix="/listings", tags=["listings"])
    application.include_router(
        price_history.router, prefix="/price-history", tags=["price_history"]
    )

    return application


# The app instance that uvicorn imports
app = create_app()
