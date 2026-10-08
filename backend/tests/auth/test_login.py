from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.db.base import UserStatus
from app.db.models import AuditLog, User, UserSession


class _RecordingPasswordHasher:
    def __init__(self) -> None:
        self.delegate = PasswordHasher()
        self.verified_hashes: list[str] = []

    def verify(self, password_hash: str, password: str) -> bool:
        self.verified_hashes.append(password_hash)
        return self.delegate.verify(password_hash, password)


@pytest.mark.security
def test_auth_status_reports_uninitialized_without_creating_session(
    client: TestClient,
    app_and_engine: tuple[object, object],
) -> None:
    response = client.get("/api/v1/auth/status")

    assert response.status_code == 200
    assert response.json() == {"data": {"initialized": False}}
    assert "set-cookie" not in response.headers
    with Session(app_and_engine[1]) as session:
        assert session.scalars(select(UserSession)).all() == []


@pytest.mark.security
def test_auth_status_reports_initialized_when_any_user_exists(
    client: TestClient,
    app_and_engine: tuple[object, object],
) -> None:
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )
    with Session(app_and_engine[1]) as session:
        sessions_before = session.scalars(select(UserSession)).all()

    response = TestClient(client.app).get("/api/v1/auth/status")

    assert response.status_code == 200
    assert response.json() == {"data": {"initialized": True}}
    assert "set-cookie" not in response.headers
    with Session(app_and_engine[1]) as session:
        sessions_after = session.scalars(select(UserSession)).all()
    assert len(sessions_after) == len(sessions_before)


@pytest.mark.security
def test_initialize_first_admin_and_reject_second(client: TestClient) -> None:
    first = client.post(
        "/api/v1/auth/initialize",
        json={
            "username": "admin",
            "password": "correct horse",
            "display_name": "管理员",
        },
    )
    second = client.post(
        "/api/v1/auth/initialize",
        json={"username": "other", "password": "correct horse"},
    )

    assert first.status_code == 201
    assert first.json()["data"]["role"] == "admin"
    assert second.status_code == 409


@pytest.mark.security
def test_login_sets_secure_session_cookie_and_me(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse"},
    )
    me = client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.cookies.get("fund_session")
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "admin"


@pytest.mark.security
def test_wrong_password_uses_generic_error_and_locks_after_five_failures(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        service = AuthService(session)
        service.initialize_admin("admin", "correct horse")
        now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

        for _ in range(5):
            with pytest.raises(service.InvalidCredentials):
                service.authenticate("admin", "wrong", now=now)

        user = session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        assert user.failed_login_count == 5
        assert service._as_utc(user.locked_until) == now + timedelta(minutes=15)

        with pytest.raises(service.InvalidCredentials):
            service.authenticate("admin", "correct horse", now=now)

        result = service.authenticate(
            "admin", "correct horse", now=now + timedelta(minutes=16)
        )
        assert result.user.username == "admin"
        assert user.failed_login_count == 0
        assert user.locked_until is None


@pytest.mark.security
@pytest.mark.parametrize("account_state", ["missing", "disabled", "locked"])
def test_unusable_accounts_run_dummy_argon2id_verification(
    app_and_engine: tuple[object, object],
    account_state: str,
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        service = AuthService(session)
        user_hash: str | None = None
        if account_state != "missing":
            user = service.initialize_admin("admin", "correct horse").user
            if account_state == "disabled":
                user.status = UserStatus.DISABLED
            else:
                user.locked_until = now + timedelta(minutes=5)
            user_hash = user.password_hash
            session.flush()

        recorder = _RecordingPasswordHasher()
        service.password_hasher = recorder  # type: ignore[assignment]

        with pytest.raises(service.InvalidCredentials):
            service.authenticate("admin", "wrong password", now=now)

        assert len(recorder.verified_hashes) == 1
        assert recorder.verified_hashes[0].startswith("$argon2id$")
        if user_hash is not None:
            assert recorder.verified_hashes[0] != user_hash


@pytest.mark.security
def test_postgresql_authentication_load_locks_user_row() -> None:
    class RecordingSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        statement: object | None = None

        def scalar(self, statement: object) -> None:
            self.statement = statement

    recording_session = RecordingSession()
    service = AuthService(cast(Session, recording_session))

    service._load_user("admin")

    assert recording_session.statement is not None
    assert recording_session.statement._for_update_arg is not None  # type: ignore[attr-defined]


@pytest.mark.security
def test_disabled_user_cannot_use_existing_session(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )
    client.post(
        "/api/v1/users",
        json={"username": "viewer", "password": "correct horse", "role": "viewer"},
    )
    viewer = TestClient(client.app)
    viewer.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct horse"},
    )

    disable = client.post("/api/v1/users/2/disable")
    me = viewer.get("/api/v1/auth/me")

    assert disable.status_code == 200
    assert me.status_code == 401


@pytest.mark.security
def test_logout_and_change_password_revoke_other_sessions(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )
    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse"},
    )
    other = TestClient(client.app)
    other.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse"},
    )

    changed = client.post(
        "/api/v1/auth/change-password",
        json={"old_password": "correct horse", "new_password": "new correct horse"},
    )
    old_session_me = other.get("/api/v1/auth/me")
    logged_out = client.post("/api/v1/auth/logout")
    current_session_me = client.get("/api/v1/auth/me")

    assert changed.status_code == 200
    assert old_session_me.status_code == 401
    assert logged_out.status_code == 204
    assert current_session_me.status_code == 401


@pytest.mark.security
def test_audit_log_does_not_store_plaintext_password_or_token(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        service = AuthService(session)
        result = service.initialize_admin("admin", "correct horse")
        login = service.authenticate("admin", "correct horse")
        session.commit()

        user = session.scalar(select(User).where(User.username == "admin"))
        audit_rows = session.scalars(select(AuditLog)).all()
        sessions = session.scalars(select(UserSession)).all()

        assert user is not None
        assert user.password_hash != "correct horse"
        assert user.password_hash.startswith("$argon2id$")
        assert all("correct horse" not in str(row.summary) for row in audit_rows)
        assert sessions[0].token_hash != login.raw_token
        assert result.user.id == user.id


@pytest.mark.security
def test_production_login_cookie_is_secure(client: TestClient) -> None:
    client.app.state.settings = client.app.state.settings.__class__(
        service_name="fund-dashboard-api",
        environment="production",
        host="127.0.0.1",
        port=8000,
        database_url="sqlite+pysqlite:///:memory:",
    )
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse"},
    )

    assert "Secure" in response.headers["set-cookie"]
