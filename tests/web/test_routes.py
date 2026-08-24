"""Smoke tests for web routes."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture(autouse=True)
def _force_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the basic-auth backend for every test in this module.

    ``settings`` is a module-level singleton that loads ``AUTH_BACKEND`` from
    the developer's ``.env``. Without this, running the suite while ``.env`` has
    ``AUTH_BACKEND=clerk`` (e.g. mid-Clerk-testing) makes these tests exercise
    the Clerk path: the ``client`` fixture's HTTP Basic credentials don't
    authenticate, so the 401 becomes a 302 redirect to ``/sign-in`` and the
    assertions below break. The backend is read at request time, so patching
    the singleton is enough. See ``tests/auth/test_basic.py`` for the same
    pattern.
    """
    monkeypatch.setattr(settings, "auth_backend", "basic")


def test_home_page(client: TestClient) -> None:
    """Home page loads successfully."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


def test_vehicles_page(client: TestClient) -> None:
    """Vehicles page loads successfully."""
    response = client.get("/vehicles")
    assert response.status_code == 200
    assert "Vehicles" in response.text


def test_searches_page(client: TestClient) -> None:
    """Searches page loads successfully."""
    response = client.get("/searches")
    assert response.status_code == 200
    assert "Search" in response.text


def test_listings_page(client: TestClient) -> None:
    """Listings page loads successfully."""
    response = client.get("/listings")
    assert response.status_code == 200
    assert "Listings" in response.text
