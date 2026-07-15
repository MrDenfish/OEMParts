"""Tests for basic auth."""

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture(autouse=True)
def force_basic_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the basic-auth backend for every test in this module.

    ``settings`` is a module-level singleton that loads ``AUTH_BACKEND`` from
    the developer's ``.env``. Without this, running the suite while ``.env`` has
    ``AUTH_BACKEND=clerk`` (e.g. mid-Clerk-testing) makes these tests exercise
    the Clerk path: the 401 becomes a 302 redirect to ``/sign-in`` that the
    TestClient follows, so ``GET /`` returns 200 and the assertions break.
    The backend is read at request time, so patching the singleton is enough.
    """
    monkeypatch.setattr(settings, "auth_backend", "basic")


def test_valid_credentials(client: TestClient) -> None:
    """Valid basic auth credentials return 200."""
    response = client.get("/")
    assert response.status_code == 200


def test_invalid_credentials(client: TestClient) -> None:
    """Invalid credentials return 401."""
    # Override auth on the client for this test
    client.auth = ("wrong", "credentials")
    response = client.get("/")
    assert response.status_code == 401


def test_no_credentials() -> None:
    """No credentials returns 401 with WWW-Authenticate header."""
    from app.web.main import app

    unauthenticated_client = TestClient(app, raise_server_exceptions=False)
    response = unauthenticated_client.get("/")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
