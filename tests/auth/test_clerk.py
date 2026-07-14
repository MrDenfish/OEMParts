"""Unit tests for the Clerk auth backend's user provisioning.

The Clerk Backend API client is mocked, so these tests never hit the network.
They are skipped when ``clerk-backend-api`` is not installed (Phase 1
environments), and activate automatically once the dependency is present.
"""

import pytest
from sqlalchemy.orm import Session

pytest.importorskip("clerk_backend_api")

from app.auth import clerk  # noqa: E402  (import after importorskip guard)
from app.db.models import User  # noqa: E402


class _FakeEmail:
    def __init__(self, id_: str, email_address: str) -> None:
        self.id = id_
        self.email_address = email_address


class _FakeClerkUser:
    def __init__(self, primary_id: str, emails: list[_FakeEmail]) -> None:
        self.primary_email_address_id = primary_id
        self.email_addresses = emails


class _FakeUsers:
    def __init__(self, user: _FakeClerkUser) -> None:
        self._user = user

    def get(self, user_id: str) -> _FakeClerkUser:
        return self._user


class _FakeClient:
    def __init__(self, user: _FakeClerkUser) -> None:
        self.users = _FakeUsers(user)


def test_provision_user_creates_and_uses_primary_email(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First login creates a User with the Clerk id and the primary email."""
    fake = _FakeClient(
        _FakeClerkUser(
            primary_id="idn_primary",
            emails=[
                _FakeEmail("idn_other", "old@example.com"),
                _FakeEmail("idn_primary", "primary@example.com"),
            ],
        )
    )
    monkeypatch.setattr(clerk, "_client", lambda: fake)

    user = clerk.provision_user(db_session, "user_abc123")

    assert user.auth_provider_id == "user_abc123"
    assert user.email == "primary@example.com"
    assert (
        db_session.query(User).filter(User.auth_provider_id == "user_abc123").count()
        == 1
    )


def test_provision_user_is_idempotent_for_existing_user(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A returning user is looked up by Clerk id; the API is never called."""
    existing = User(auth_provider_id="user_xyz", email="existing@example.com")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    def _should_not_be_called() -> _FakeClient:
        raise AssertionError("Clerk API must not be called for an existing user")

    monkeypatch.setattr(clerk, "_client", _should_not_be_called)

    user = clerk.provision_user(db_session, "user_xyz")

    assert user.id == existing.id
    assert db_session.query(User).count() == 1


def test_provision_user_falls_back_to_first_email(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no address matches the primary id, the first email is used."""
    fake = _FakeClient(
        _FakeClerkUser(
            primary_id="idn_missing",
            emails=[_FakeEmail("idn_a", "first@example.com")],
        )
    )
    monkeypatch.setattr(clerk, "_client", lambda: fake)

    user = clerk.provision_user(db_session, "user_nomatch")

    assert user.email == "first@example.com"
