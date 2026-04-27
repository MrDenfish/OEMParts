"""Shared test fixtures for OEMParts.

Uses the real local Postgres database (oemparts_test) for integration tests.
Falls back to the main database with a test-prefixed schema if oemparts_test
is not available.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base, Search, User, Vehicle


# Build a test database URL — use the same Postgres instance, different DB name
TEST_DB_URL = (
    f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
    f"@{settings.postgres_host}:{settings.postgres_port}/oemparts_test"
)

# Connect to the dedicated test database.
# NEVER fall back to the main DB — drop_all would destroy real data.
_test_engine = create_engine(TEST_DB_URL, echo=False)

_TestSessionLocal = sessionmaker(bind=_test_engine, expire_on_commit=False)


@pytest.fixture()
def db_session() -> Session:
    """Provide a clean database session for each test.

    Creates all tables before the test and drops them after.
    """
    Base.metadata.create_all(bind=_test_engine)
    session = _TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=_test_engine)


@pytest.fixture()
def test_user(db_session: Session) -> User:
    """Create and return a test user."""
    user = User(email="testuser@example.com")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def test_vehicle(db_session: Session, test_user: User) -> Vehicle:
    """Create and return a test vehicle."""
    vehicle = Vehicle(
        user_id=test_user.id,
        year=2012,
        make="Land Rover",
        model="LR4",
        nickname="Test Vehicle",
    )
    db_session.add(vehicle)
    db_session.commit()
    db_session.refresh(vehicle)
    return vehicle


@pytest.fixture()
def test_search(db_session: Session, test_user: User, test_vehicle: Vehicle) -> Search:
    """Create and return a test search."""
    search = Search(
        user_id=test_user.id,
        vehicle_id=test_vehicle.id,
        query_text="LR4 coolant crossover pipe",
        oem_number="LR010819",
        is_active=True,
    )
    db_session.add(search)
    db_session.commit()
    db_session.refresh(search)
    return search


@pytest.fixture()
def client(db_session: Session, test_user: User) -> TestClient:
    """Provide an authenticated test client."""
    from app.db.session import get_db
    from app.web.main import app

    # Override the get_db dependency to use the test session
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    test_client = TestClient(app)
    # Set basic auth credentials
    test_client.auth = (settings.basic_auth_username, settings.basic_auth_password)

    yield test_client

    app.dependency_overrides.clear()
