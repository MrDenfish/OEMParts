"""Application configuration via Pydantic Settings.

Loads values from .env file. See .env.example for all available settings.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
