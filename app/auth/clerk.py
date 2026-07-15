"""Phase 2: Clerk auth backend.

Active only when ``AUTH_BACKEND=clerk``. On each request we verify Clerk's
``__session`` cookie via the Clerk Backend API (networkless JWT verification
after a one-time JWKS fetch), then map the Clerk user id (the JWT ``sub``
claim) to a local :class:`~app.db.models.User` row.

``dependencies.py`` dispatches to this module based on ``settings.auth_backend``;
nothing here runs while the app is on the Phase 1 basic-auth backend.
"""

import logging

import httpx
from clerk_backend_api import Clerk
from clerk_backend_api.security.types import AuthenticateRequestOptions
from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import User

logger = logging.getLogger(__name__)

# Lazily-constructed singleton Clerk client. Built on first use so that
# basic-auth deployments (no secret key configured) never touch it.
_clerk_client: Clerk | None = None


def _client() -> Clerk:
    """Return the shared Clerk Backend API client, constructing it on first use."""
    global _clerk_client
    if _clerk_client is None:
        if not settings.clerk_secret_key:
            raise RuntimeError("AUTH_BACKEND=clerk but CLERK_SECRET_KEY is not set.")
        _clerk_client = Clerk(bearer_auth=settings.clerk_secret_key)
    return _clerk_client


def authenticate_clerk(request: Request) -> str:
    """Verify the request's Clerk session and return the Clerk user id (``sub``).

    Raises ``HTTPException(401)`` when the request carries no valid session.
    """
    # The SDK expects an httpx.Request. Only the headers matter for
    # verification (the Authorization bearer and the __session cookie).
    httpx_request = httpx.Request(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
    )

    state = _client().authenticate_request(
        httpx_request,
        AuthenticateRequestOptions(
            authorized_parties=settings.clerk_authorized_parties_list,
        ),
    )

    if not state.is_signed_in:
        # state.reason is an enum-like value; log only its short label so we
        # never write token material to the logs.
        logger.info(
            "Clerk auth rejected: %s", getattr(state.reason, "name", state.reason)
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    sub = state.payload.get("sub") if state.payload else None
    if not sub:
        logger.warning("Clerk session verified but no 'sub' claim present.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session",
        )
    return sub


def provision_user(db: Session, clerk_user_id: str) -> User:
    """Return the local User for a Clerk user id, creating it on first login.

    The email is not present in the session JWT, so on first login we fetch it
    from the Clerk Backend API to satisfy the ``User.email`` NOT NULL constraint.
    """
    user = db.query(User).filter(User.auth_provider_id == clerk_user_id).first()
    if user is not None:
        return user

    email = _fetch_primary_email(clerk_user_id)
    logger.info("Provisioning new user for Clerk id %s***", clerk_user_id[:8])
    user = User(auth_provider_id=clerk_user_id, email=email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _fetch_primary_email(clerk_user_id: str) -> str:
    """Fetch the user's primary email address from the Clerk Backend API."""
    clerk_user = _client().users.get(user_id=clerk_user_id)
    primary_id = getattr(clerk_user, "primary_email_address_id", None)
    emails = getattr(clerk_user, "email_addresses", None) or []

    for addr in emails:
        if getattr(addr, "id", None) == primary_id:
            return addr.email_address
    if emails:
        return emails[0].email_address

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Clerk account has no email address",
    )
