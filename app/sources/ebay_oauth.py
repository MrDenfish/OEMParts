"""eBay OAuth client-credentials token management.

Three-tier caching strategy:
  1. In-memory module-level cache (fastest, lost on restart)
  2. DB fallback via oauth_tokens table (survives restarts)
  3. Fresh fetch from eBay OAuth endpoint (last resort)

Token lifetime is ~2 hours. We refresh 5 minutes early to avoid
mid-request expiry.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import OAuthToken, utcnow

logger = logging.getLogger(__name__)

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SANDBOX_OAUTH_URL = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"

# Safety margin: refresh token 5 minutes before actual expiry
TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)


@dataclass
class CachedToken:
    access_token: str
    expires_at: datetime


# Module-level in-memory cache (single token for the app)
_token_cache: CachedToken | None = None


def _get_oauth_url() -> str:
    """Return the correct OAuth URL based on environment config."""
    if settings.ebay_env == "sandbox":
        return EBAY_SANDBOX_OAUTH_URL
    return EBAY_OAUTH_URL


def _token_is_valid(token: CachedToken) -> bool:
    """Check if token is still valid with safety margin."""
    return token.expires_at > (utcnow() + TOKEN_EXPIRY_MARGIN)


def _fetch_token_from_ebay() -> CachedToken:
    """Request a new token via client credentials grant.

    Raises httpx.HTTPStatusError on non-2xx response.
    """
    logger.info("Fetching new eBay OAuth token")

    with httpx.Client() as client:
        response = client.post(
            _get_oauth_url(),
            data={
                "grant_type": "client_credentials",
                "scope": settings.ebay_oauth_scope,
            },
            auth=(settings.ebay_client_id, settings.ebay_client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )
        response.raise_for_status()

    data = response.json()
    expires_in = int(data["expires_in"])
    token = CachedToken(
        access_token=data["access_token"],
        expires_at=utcnow() + timedelta(seconds=expires_in),
    )

    # Log success without exposing the full token
    token_preview = token.access_token[:4] + "..." + token.access_token[-4:]
    logger.info(
        "eBay OAuth token acquired: %s, expires in %d seconds",
        token_preview,
        expires_in,
    )
    return token


def _load_token_from_db(db: Session) -> CachedToken | None:
    """Load the most recent non-expired token from the oauth_tokens table."""
    row = (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "ebay")
        .order_by(OAuthToken.created_at.desc())
        .first()
    )
    if row is None:
        return None

    token = CachedToken(
        access_token=row.access_token,
        expires_at=row.expires_at,
    )
    if _token_is_valid(token):
        logger.info("Loaded valid eBay token from DB (cold start recovery)")
        return token

    return None


def _save_token_to_db(db: Session, token: CachedToken) -> None:
    """Persist token to oauth_tokens for cold-start recovery."""
    row = OAuthToken(
        provider="ebay",
        access_token=token.access_token,
        expires_at=token.expires_at,
    )
    db.add(row)
    db.flush()


def get_ebay_token(db: Session) -> str:
    """Return a valid eBay access token string.

    Checks in-memory cache, then DB, then fetches new from eBay.
    """
    global _token_cache  # noqa: PLW0603

    # 1. Check in-memory cache
    if _token_cache and _token_is_valid(_token_cache):
        return _token_cache.access_token

    # 2. Check DB fallback (cold start)
    db_token = _load_token_from_db(db)
    if db_token and _token_is_valid(db_token):
        _token_cache = db_token
        return db_token.access_token

    # 3. Fetch new token from eBay
    new_token = _fetch_token_from_ebay()
    _token_cache = new_token
    _save_token_to_db(db, new_token)
    return new_token.access_token
