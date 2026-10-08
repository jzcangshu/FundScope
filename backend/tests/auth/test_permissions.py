import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.db.base import UserRole, UserStatus


@pytest.mark.security
def test_only_admin_can_manage_accounts(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )
    client.post(
        "/api/v1/users",
        json={"username": "operator", "password": "correct horse", "role": "operator"},
    )
    client.post(
        "/api/v1/users",
        json={"username": "viewer", "password": "correct horse", "role": "viewer"},
    )

    operator = TestClient(client.app)
    viewer = TestClient(client.app)
    operator.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": "correct horse"},
    )
    viewer.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct horse"},
    )

    operator_attempt = operator.post(
        "/api/v1/users",
        json={"username": "blocked", "password": "correct horse", "role": "viewer"},
    )
    viewer_attempt = viewer.post(
        "/api/v1/users",
        json={"username": "blocked2", "password": "correct horse", "role": "viewer"},
    )

    assert operator_attempt.status_code == 403
    assert viewer_attempt.status_code == 403


@pytest.mark.security
def test_admin_can_change_role_reset_enable_and_revoke_sessions(
    client: TestClient,
) -> None:
    client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )
    created = client.post(
        "/api/v1/users",
        json={"username": "member", "password": "correct horse", "role": "viewer"},
    )
    user_id = created.json()["data"]["id"]
    member = TestClient(client.app)
    member.post(
        "/api/v1/auth/login",
        json={"username": "member", "password": "correct horse"},
    )

    changed_role = client.patch(
        f"/api/v1/users/{user_id}/role", json={"role": "operator"}
    )
    revoked = client.post(f"/api/v1/users/{user_id}/revoke-sessions")
    disabled = client.post(f"/api/v1/users/{user_id}/disable")
    enabled = client.post(f"/api/v1/users/{user_id}/enable")
    reset = client.post(
        f"/api/v1/users/{user_id}/reset-password",
        json={"password": "replacement password"},
    )

    assert changed_role.json()["data"]["role"] == "operator"
    assert revoked.status_code == 200
    assert member.get("/api/v1/auth/me").status_code == 401
    assert disabled.json()["data"]["status"] == "disabled"
    assert enabled.json()["data"]["status"] == "active"
    assert reset.status_code == 200


@pytest.mark.security
def test_admin_cannot_disable_self(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        service = AuthService(session)
        actor = service.initialize_admin("admin", "correct horse").user

        with pytest.raises(
            service.AccountProtection, match="admin_cannot_disable_self"
        ):
            service.set_user_status(actor, actor.id, UserStatus.DISABLED)

        assert actor.status == UserStatus.ACTIVE


@pytest.mark.security
def test_admin_cannot_downgrade_self(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        service = AuthService(session)
        actor = service.initialize_admin("admin", "correct horse").user

        with pytest.raises(
            service.AccountProtection, match="admin_cannot_downgrade_self"
        ):
            service.change_role(actor, actor.id, UserRole.OPERATOR)

        assert actor.role == UserRole.ADMIN


@pytest.mark.security
@pytest.mark.parametrize("operation", ["disable", "downgrade"])
def test_last_active_admin_cannot_be_removed(
    app_and_engine: tuple[object, object],
    operation: str,
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        service = AuthService(session)
        active_admin = service.initialize_admin("admin", "correct horse").user
        inactive_admin = service.create_user(
            active_admin,
            "inactive-admin",
            "correct horse",
            UserRole.ADMIN,
        )
        service.set_user_status(active_admin, inactive_admin.id, UserStatus.DISABLED)
        session.commit()

        with pytest.raises(service.AccountProtection, match="last_active_admin"):
            if operation == "disable":
                service.set_user_status(
                    inactive_admin, active_admin.id, UserStatus.DISABLED
                )
            else:
                service.change_role(inactive_admin, active_admin.id, UserRole.OPERATOR)

        assert active_admin.status == UserStatus.ACTIVE
        assert active_admin.role == UserRole.ADMIN
