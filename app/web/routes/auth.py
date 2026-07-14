"""Phase 2: Clerk sign-in page.

Only meaningful when ``AUTH_BACKEND=clerk``. Renders a page that mounts Clerk's
embedded sign-in widget. This route is intentionally *not* protected by
``get_current_user`` — it is where unauthenticated users land. Sign-out is
handled client-side by Clerk's ``UserButton`` in the sidebar.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.main import templates

router = APIRouter()


@router.get("/sign-in", response_class=HTMLResponse)
def sign_in_page(request: Request) -> HTMLResponse:
    """Render the embedded Clerk sign-in page."""
    return templates.TemplateResponse(
        request,
        "pages/sign_in.html",
        {"active_page": "sign_in"},
    )
