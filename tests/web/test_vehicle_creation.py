"""VIN decode, model-list, and vehicle-creation routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import User, Vehicle
from app.sources.nhtsa_vpic import DecodedVin

# Import app.web.main FIRST: vehicles.py does `from app.web.main import
# templates` at module top, so importing the route module before the app
# module has fully initialized triggers a circular-import AttributeError
# (main -> vehicles -> main, mid-init). Loading main.py first (which
# defines `templates` before it wires up the routers) avoids the ordering
# trap.
import app.web.main  # noqa: F401,E402
from app.web.routes import vehicles as vehicles_module  # noqa: E402


@pytest.fixture(autouse=True)
def _force_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the basic-auth backend (and AI filter off) for every test here.

    See tests/web/test_search_create_category.py for why this is needed:
    a developer .env with AUTH_BACKEND=clerk or AI_FILTER_ENABLED=true would
    otherwise silently redirect these tests onto the wrong path.
    """
    monkeypatch.setattr(settings, "auth_backend", "basic")
    monkeypatch.setattr(settings, "ai_filter_enabled", False)


@pytest.fixture()
def authed_client(db_session: Session, test_user: User) -> TestClient:
    """Provide an authenticated test client as the test_user."""
    from app.auth.dependencies import get_current_user
    from app.db.session import get_db
    from app.web.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    def override_get_current_user():
        return test_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    test_client = TestClient(app)

    yield test_client

    app.dependency_overrides.clear()


# --- POST /vehicles/decode-vin ------------------------------------------------


def test_decode_vin_success_returns_decoded_fields(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vehicles_module,
        "decode_vin",
        lambda db, vin: DecodedVin(2012, "LAND ROVER", "LR4", "HSE", "SUV"),
    )
    response = authed_client.post(
        "/vehicles/decode-vin", data={"vin_lookup": "SALAG2D40CA000000"}
    )
    assert response.status_code == 200
    assert 'value="2012"' in response.text
    assert 'value="LAND ROVER"' in response.text
    assert 'value="LR4"' in response.text
    assert 'name="vin"' in response.text


def test_decode_vin_failure_falls_back_to_dropdown(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vehicles_module, "decode_vin", lambda db, vin: None)
    response = authed_client.post(
        "/vehicles/decode-vin", data={"vin_lookup": "not-a-real-vin"}
    )
    assert response.status_code == 200
    assert "Could not decode" in response.text
    assert "<select" in response.text


# --- GET /vehicles ---------------------------------------------------


def test_vehicles_page_contains_vin_input_and_decode_button(
    authed_client: TestClient,
) -> None:
    """Page has VIN input (name=vin_lookup) and button that posts to /vehicles/decode-vin."""
    response = authed_client.get("/vehicles")
    assert response.status_code == 200
    assert 'name="vin_lookup"' in response.text
    assert 'hx-post="/vehicles/decode-vin"' in response.text
    assert "Decode VIN" in response.text


def test_vehicles_page_contains_year_make_selects_and_fields_container(
    authed_client: TestClient,
) -> None:
    """Page contains Year and Make selects with options, and #vehicle-fields container."""
    response = authed_client.get("/vehicles")
    assert response.status_code == 200
    # Year select and options
    assert '<select id="year"' in response.text
    # Make select and "Land Rover" option
    assert '<select id="make"' in response.text
    assert 'value="Land Rover"' in response.text
    # Container for fields
    assert 'id="vehicle-fields"' in response.text


def test_vehicles_page_contains_nickname_input(
    authed_client: TestClient,
) -> None:
    """Page still has nickname input."""
    response = authed_client.get("/vehicles")
    assert response.status_code == 200
    assert 'name="nickname"' in response.text


# --- GET /vehicles/models ------------------------------------------------------


def test_model_options_lists_models_plus_other(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vehicles_module,
        "get_models_for_make_year",
        lambda db, make, year: ["LR2", "LR4"],
    )
    response = authed_client.get("/vehicles/models?make=Land+Rover&year=2012")
    assert response.status_code == 200
    assert 'value="LR2"' in response.text
    assert 'value="LR4"' in response.text
    assert 'value="__other__"' in response.text


def test_model_options_none_yields_only_other_option(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vehicles_module, "get_models_for_make_year", lambda db, make, year: None
    )
    response = authed_client.get("/vehicles/models?make=Land+Rover&year=2012")
    assert response.status_code == 200
    assert 'value="LR2"' not in response.text
    assert 'value="LR4"' not in response.text
    assert 'value="__other__"' in response.text
    assert "not listed" in response.text


def test_model_options_tolerates_empty_year_string(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Make select's hx-get fires with year="" before a year is picked.

    year: int = 0 would 422 here (FastAPI only applies int defaults when
    the param is absent, not when it's ""); this must be a clean 200 with
    only the fallback option, and NHTSA must not be called.
    """

    def _fail_if_called(db, make, year):
        raise AssertionError("get_models_for_make_year should not be called")

    monkeypatch.setattr(vehicles_module, "get_models_for_make_year", _fail_if_called)
    response = authed_client.get("/vehicles/models?make=Land+Rover&year=")
    assert response.status_code == 200
    assert 'value="__other__"' in response.text
    assert 'value="LR2"' not in response.text


# --- POST /vehicles ------------------------------------------------------------


def test_create_with_other_model_uses_model_text(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    response = authed_client.post(
        "/vehicles",
        data={
            "year": "2012",
            "make": "Land Rover",
            "model": "__other__",
            "model_text": "LR4",
        },
    )
    assert response.status_code == 200
    vehicle = db_session.query(Vehicle).filter_by(user_id=test_user.id).one()
    assert vehicle.make == "Land Rover"
    assert vehicle.model == "LR4"


def test_create_with_make_text_and_model_text_overrides_dropdowns(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    response = authed_client.post(
        "/vehicles",
        data={
            "year": "2023",
            "make": "",
            "model": "",
            "make_text": "Koenigsegg",
            "model_text": "CC850",
        },
    )
    assert response.status_code == 200
    vehicle = db_session.query(Vehicle).filter_by(user_id=test_user.id).one()
    assert vehicle.make == "Koenigsegg"
    assert vehicle.model == "CC850"


def test_create_stores_valid_vin(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    response = authed_client.post(
        "/vehicles",
        data={
            "year": "2012",
            "make_text": "Land Rover",
            "model_text": "LR4",
            "vin": "SALAG2D40CA000000",
        },
    )
    assert response.status_code == 200
    vehicle = db_session.query(Vehicle).filter_by(user_id=test_user.id).one()
    assert vehicle.vin == "SALAG2D40CA000000"


def test_create_with_invalid_vin_stores_null(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    response = authed_client.post(
        "/vehicles",
        data={
            "year": "2012",
            "make_text": "Land Rover",
            "model_text": "LR4",
            "vin": "short",
        },
    )
    assert response.status_code == 200
    vehicle = db_session.query(Vehicle).filter_by(user_id=test_user.id).one()
    assert vehicle.vin is None


def test_create_with_no_resolvable_model_creates_nothing(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    response = authed_client.post(
        "/vehicles",
        data={
            "year": "2012",
            "make_text": "Land Rover",
            "model": "",
            "model_text": "",
        },
    )
    assert response.status_code == 200
    assert db_session.query(Vehicle).filter_by(user_id=test_user.id).count() == 0
    assert response.headers.get("HX-Retarget") == "#vehicle-fields"
    assert "Make and model are required" in response.text


def test_create_plain_full_form_post_regression(
    authed_client: TestClient, db_session: Session, test_user: User
) -> None:
    """Old-style POST (year/make/model resolved directly, no VIN/dropdowns) still works."""
    response = authed_client.post(
        "/vehicles",
        data={"year": "2012", "make": "Land Rover", "model": "LR4"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    vehicle = db_session.query(Vehicle).filter_by(user_id=test_user.id).one()
    assert vehicle.make == "Land Rover"
    assert vehicle.model == "LR4"
    # HTMX request returns the new row partial.
    assert f'id="vehicle-{vehicle.id}"' in response.text
