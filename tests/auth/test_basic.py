"""Tests for basic auth."""

from fastapi.testclient import TestClient


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
