"""Database session factory.

Provides two entry points:
  - get_db()      : FastAPI dependency (yields session, no auto-commit)
  - get_session() : Context manager for CLI/worker (auto-commits on clean exit)

Both use the same SessionLocal factory bound to the configured database URL.
"""

import logging
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.effective_database_url,
    echo=False,
    pool_pre_ping=True,  # Reconnect on stale connections after Docker restarts
)

SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,  # Prevent lazy-load issues after commit
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for request-scoped sessions.

    Does NOT auto-commit. Route handlers commit explicitly after
    successful writes. This keeps commit control explicit in routes.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager for CLI/worker code.

    Auto-commits on clean exit. Rolls back on exception.
    Usage: with get_session() as db: ...
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
