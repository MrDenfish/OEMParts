"""Application configuration via Pydantic Settings.

Loads values from .env file. See .env.example for all available settings.
"""

import base64
import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5442
    postgres_db: str = "oemparts"
    postgres_user: str = "oemparts"
    postgres_password: str = "changeme_secure_password_here"
    database_url: str = ""

    # Logging
    log_level: str = "INFO"

    # eBay API
    ebay_env: str = "production"
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_US"
    ebay_oauth_scope: str = "https://api.ebay.com/oauth/api_scope"

    # eBay Partner Network
    epn_campaign_id: str = ""
    epn_enabled: bool = False

    # NHTSA
    nhtsa_vpic_base_url: str = "https://vpic.nhtsa.dot.gov/api"

    # Auth
    auth_backend: str = "basic"
    basic_auth_username: str = "admin"
    basic_auth_password: str = ""
    session_secret_key: str = ""

    # Clerk (Phase 2 — used only when auth_backend == "clerk")
    clerk_secret_key: str = ""
    clerk_publishable_key: str = ""
    # Comma-separated list of allowed origins (CSRF protection), e.g.
    # "http://localhost:8000,https://oempartsagent.com"
    clerk_authorized_parties: str = ""

    # Fetching
    fetch_default_ttl_minutes: int = 240
    fetch_max_listings_per_query: int = 50
    fetch_api_rate_limit_per_min: int = 30

    # Alerts (Phase 3)
    alerts_enabled: bool = False

    # Cleanup
    listing_inactive_after_missing_cycles: int = 3
    listing_archive_after_days: int = 180

    @property
    def effective_database_url(self) -> str:
        """Return DATABASE_URL if set, otherwise build from components."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def clerk_authorized_parties_list(self) -> list[str]:
        """Parse CLERK_AUTHORIZED_PARTIES (comma-separated) into a list."""
        return [
            part.strip()
            for part in self.clerk_authorized_parties.split(",")
            if part.strip()
        ]

    @property
    def clerk_frontend_api(self) -> str:
        """Derive the Clerk Frontend API host from the publishable key.

        A Clerk publishable key is `pk_test_<b64>` or `pk_live_<b64>` where the
        base64 payload decodes to `<frontend-api-host>$`. We decode it so the
        template can build the ClerkJS <script> src without a second env var.
        Returns "" if the key is absent or unparsable.
        """
        key = self.clerk_publishable_key
        if not key:
            return ""
        try:
            # Strip the "pk_test_" / "pk_live_" prefix, leaving the b64 payload.
            payload = key.split("_", 2)[2]
            # base64 may need padding restored before decoding.
            padded = payload + "=" * (-len(payload) % 4)
            decoded = base64.b64decode(padded).decode("utf-8")
            return decoded.rstrip("$")
        except (IndexError, ValueError):
            logger.warning(
                "Could not parse Frontend API host from CLERK_PUBLISHABLE_KEY"
            )
            return ""


settings = Settings()
