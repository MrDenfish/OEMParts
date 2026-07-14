"""FastAPI application factory and startup configuration.

Configures Jinja2 templates, static files, session middleware, and route mounting.
Run with: PYTHONPATH=$PWD uvicorn app.web.main:app --reload --port 8000
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
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

# Auth-related globals so base.html can conditionally load ClerkJS.
templates.env.globals["auth_backend"] = settings.auth_backend
templates.env.globals["clerk_publishable_key"] = settings.clerk_publishable_key
templates.env.globals["clerk_frontend_api"] = settings.clerk_frontend_api


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

    # Under the Clerk backend, a 401 from a protected page should send the
    # browser to the embedded sign-in page rather than surface a bare error.
    # (In basic mode we leave 401s alone so the native HTTP Basic prompt fires.)
    @application.exception_handler(StarletteHTTPException)
    async def auth_aware_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and settings.auth_backend == "clerk"
        ):
            # HTMX requests can't follow a 302 body-swap; use HX-Redirect.
            if request.headers.get("HX-Request"):
                resp = Response(status_code=status.HTTP_204_NO_CONTENT)
                resp.headers["HX-Redirect"] = "/sign-in"
                return resp
            return RedirectResponse("/sign-in", status_code=status.HTTP_302_FOUND)
        return await http_exception_handler(request, exc)

    # Include route modules
    from app.web.routes import auth, home, listings, price_history, searches, vehicles

    application.include_router(auth.router)
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
