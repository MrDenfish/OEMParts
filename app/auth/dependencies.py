"""FastAPI auth dependency injection.

Provides get_current_user() — the single dependency that all protected
routes use to get the authenticated User object. This is the pluggable
auth contract: Phase 1 uses basic.py, Phase 2 swaps to clerk.py, but
route handlers always call Depends(get_current_user) and get a User back.

The active backend is selected by ``settings.auth_backend`` ("basic" | "clerk").
"""

import logging

from fastapi import Depends, Request
from fastapi.security import HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.auth.basic import authenticate as basic_authenticate
from app.auth.basic import security as basic_security
from app.config import settings
from app.db.models import User
from app.db.session import get_db

logger = logging.getLogger(__name__)


def get_current_user(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(basic_security),
    db: Session = Depends(get_db),
) -> User:
    """Return the User object for the authenticated request.

    Dispatches on ``settings.auth_backend``:

    * ``"clerk"`` — verify the Clerk session and provision/look-up the User by
      the Clerk id (``auth_provider_id``).
    * ``"basic"`` (default) — HTTP Basic; the username doubles as the email and
      the user is auto-created on first sight (single-user convenience).

    The ``credentials`` dependency is always declared so the HTTP Basic scheme
    is available in basic mode; with ``auto_error=False`` it is simply ``None``
    (and unused) under the Clerk backend.
    """
    if settings.auth_backend == "clerk":
        # Imported lazily so the clerk-backend-api dependency is only required
        # when the Clerk backend is actually selected.
        from app.auth.clerk import authenticate_clerk, provision_user

        clerk_user_id = authenticate_clerk(request)
        return provision_user(db, clerk_user_id)

    # Phase 1 default: HTTP Basic. In basic mode the username is the email.
    username = basic_authenticate(request, credentials)
    user = db.query(User).filter(User.email == username).first()
    if user is None:
        logger.info("Auto-creating user for: %s", username)
        user = User(email=username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
