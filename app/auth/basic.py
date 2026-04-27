"""Phase 1: single-user basic auth backend.

Flow:
  1. Check for a valid session cookie (set on previous successful auth).
  2. If no cookie, challenge with HTTP Basic (browser shows native prompt).
  3. On success, set a signed session cookie so the prompt doesn't repeat.

Credentials come from BASIC_AUTH_USERNAME / BASIC_AUTH_PASSWORD in .env.
"""

import logging
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBasic(auto_error=False)


def verify_credentials(credentials: HTTPBasicCredentials) -> bool:
    """Constant-time comparison of username and password."""
    correct_username = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.basic_auth_username.encode("utf-8"),
    )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.basic_auth_password.encode("utf-8"),
    )
    return correct_username and correct_password


def authenticate(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    """Authenticate the request. Returns the username on success.

    Checks the session cookie first; falls back to HTTP Basic if no cookie.
    On successful HTTP Basic auth, sets a session cookie for future requests.
    """
    # Check session cookie first
    username = request.session.get("username")
    if username:
        return username

    # No session cookie — require HTTP Basic credentials
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Basic"},
        )

    if not verify_credentials(credentials):
        logger.warning(
            "Failed basic auth attempt for user: %s",
            credentials.username[:4] + "***" if credentials.username else "???",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Set session cookie so the browser prompt doesn't repeat
    request.session["username"] = credentials.username
    return credentials.username
