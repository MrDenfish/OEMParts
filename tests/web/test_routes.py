"""Smoke tests for web routes."""

from fastapi.testclient import TestClient


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
