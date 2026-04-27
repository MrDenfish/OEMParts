"""FastAPI auth dependency injection.

Provides get_current_user() — the single dependency that all protected
routes use to get the authenticated User object. This is the pluggable
auth contract: Phase 1 uses basic.py, Phase 2 swaps to clerk.py, but
route handlers always call Depends(get_current_user) and get a User back.
"""

import logging

from fastapi import Depends
from sqlalchemy.orm import Session

from app.auth.basic import authenticate
from app.db.models import User
from app.db.session import get_db

logger = logging.getLogger(__name__)


def get_current_user(
    username: str = Depends(authenticate),
    db: Session = Depends(get_db),
) -> User:
    """Return the User object for the authenticated user.

    In Phase 1, the username from HTTP Basic doubles as the email.
    Auto-creates the user in the DB if not found (single-user convenience).
    Phase 2 (Clerk) will replace this with a JWT-based lookup.
    """
    user = db.query(User).filter(User.email == username).first()
    if user is None:
        logger.info("Auto-creating user for: %s", username)
        user = User(email=username)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
