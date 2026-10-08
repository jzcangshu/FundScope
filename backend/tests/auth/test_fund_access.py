"""Account scope regression tests: grants, administrative boundaries and revocation."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.auth.fund_access import (
    require_fund_access,
    require_unrestricted_access,
    visible_fund_ids,
)
from app.auth.service import AuthService
from app.catalog.fund_removal import remove_fund
from app.db.base import FundAccessMode, UserRole
from app.db.models import AuditLog, Fund, User, UserFundAccess

pytestmark = pytest.mark.security


@pytest.fixture()
def funds(app_and_engine: tuple[object, object]) -> list[int]:
    with Session(app_and_engine[1]) as session:
        products = [Fund(standard_name=name) for name in ("Visible", "Hidden", "Other")]
        session.add_all(products)
        session.commit()
        return [product.id for product in products]


def initialize(client: TestClient) -> None:
    assert (
        client.post(
            "/api/v1/auth/initialize",
            json={"username": "admin", "password": "correct horse"},
        ).status_code
        == 201
    )


def create_member(client: TestClient, **scope: object) -> dict[str, object]:
    response = client.post(
        "/api/v1/users",
        json={
            "username": "member",
            "password": "correct horse",
            "role": "viewer",
            **scope,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.parametrize("role", ["viewer", "operator", "boss"])
def test_create_scoped_account_deduplicates_and_exposes_grants(
    client: TestClient, funds: list[int], role: str
) -> None:
    initialize(client)
    user = create_member(
        client,
        role=role,
        fund_access_mode="selected",
        fund_ids=[funds[1], funds[0], funds[1]],
    )
    assert user["fund_access_mode"] == "selected"
    assert user["fund_ids"] == funds[:2]
    login = TestClient(client.app).post(
        "/api/v1/auth/login", json={"username": "member", "password": "correct horse"}
    )
    assert login.json()["data"]["fund_ids"] == funds[:2]


def test_legacy_creation_defaults_to_all(client: TestClient) -> None:
    initialize(client)
    user = create_member(client)
    assert user["fund_access_mode"] == "all"
    assert user["fund_ids"] == []


@pytest.mark.parametrize(
    ("scope", "status"),
    [
        ({"fund_access_mode": "selected", "fund_ids": [99999]}, 400),
        ({"fund_access_mode": "all", "fund_ids": [1]}, 400),
        ({"fund_access_mode": "selected", "fund_ids": [], "role": "admin"}, 400),
        ({"fund_access_mode": "selected", "fund_ids": [0]}, 422),
        ({"fund_access_mode": "selected", "fund_ids": [True]}, 422),
        ({"fund_access_mode": "selected", "fund_ids": ["1"]}, 422),
        ({"fund_access_mode": "anything", "fund_ids": []}, 422),
    ],
)
def test_invalid_scope_does_not_create_user(
    client: TestClient, funds: list[int], scope: dict[str, object], status: int
) -> None:
    initialize(client)
    response = client.post(
        "/api/v1/users",
        json={
            "username": "member",
            "password": "correct horse",
            "role": "viewer",
            **scope,
        },
    )
    assert response.status_code == status
    assert client.get("/api/v1/users").json()["meta"]["total"] == 1


@pytest.mark.parametrize("role", ["viewer", "operator", "boss"])
def test_non_admin_cannot_grant_themselves_products(
    client: TestClient, funds: list[int], role: str
) -> None:
    initialize(client)
    user = create_member(
        client, role=role, fund_access_mode="selected", fund_ids=[funds[0]]
    )
    member = TestClient(client.app)
    member.post(
        "/api/v1/auth/login", json={"username": "member", "password": "correct horse"}
    )
    response = member.patch(
        f"/api/v1/users/{user['id']}/fund-access",
        json={"fund_access_mode": "all", "fund_ids": [], "reason": "self grant"},
    )
    assert response.status_code == 403
    assert member.get("/api/v1/auth/me").json()["data"]["fund_ids"] == [funds[0]]


def test_update_is_atomic_audited_and_visible_to_existing_session(
    client: TestClient, funds: list[int], app_and_engine: tuple[object, object]
) -> None:
    initialize(client)
    user = create_member(client, fund_access_mode="selected", fund_ids=funds[:2])
    member = TestClient(client.app)
    member.post(
        "/api/v1/auth/login", json={"username": "member", "password": "correct horse"}
    )
    path = f"/api/v1/users/{user['id']}/fund-access"
    invalid = client.patch(
        path,
        json={
            "fund_access_mode": "selected",
            "fund_ids": [99999],
            "reason": "bad grant",
        },
    )
    assert invalid.status_code == 400
    assert member.get("/api/v1/auth/me").json()["data"]["fund_ids"] == funds[:2]
    for reason in (None, "   "):
        rejected = client.patch(
            path,
            json={"fund_access_mode": "selected", "fund_ids": [], "reason": reason},
        )
        assert rejected.status_code == 422
    updated = client.patch(
        path,
        json={
            "fund_access_mode": "selected",
            "fund_ids": [funds[1], funds[2]],
            "reason": " renewal ",
        },
    )
    assert updated.status_code == 200
    assert member.get("/api/v1/auth/me").json()["data"]["fund_ids"] == funds[1:]
    with Session(app_and_engine[1]) as session:
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "user.change_fund_access")
        )
        assert audit is not None
        assert audit.reason == "renewal"
        assert audit.summary == {
            "before": {"fund_access_mode": "selected", "fund_ids": funds[:2]},
            "after": {"fund_access_mode": "selected", "fund_ids": funds[1:]},
        }
    cleared = client.patch(
        path,
        json={"fund_access_mode": "selected", "fund_ids": [], "reason": "access ended"},
    )
    assert cleared.status_code == 200
    current = member.get("/api/v1/auth/me").json()["data"]
    assert current["fund_access_mode"] == "selected"
    assert current["fund_ids"] == []


def test_role_changes_preserve_scope_except_admin_promotion(
    client: TestClient, funds: list[int], app_and_engine: tuple[object, object]
) -> None:
    initialize(client)
    user = create_member(client, fund_access_mode="selected", fund_ids=[funds[0]])
    path = f"/api/v1/users/{user['id']}/role"
    changed = client.patch(path, json={"role": "operator"}).json()["data"]
    assert changed["fund_ids"] == [funds[0]]
    assert changed["fund_access_mode"] == "selected"
    promoted = client.patch(path, json={"role": "admin"}).json()["data"]
    assert promoted["fund_access_mode"] == "all"
    assert promoted["fund_ids"] == []
    with Session(app_and_engine[1]) as session:
        assert (
            session.scalars(
                select(UserFundAccess).where(UserFundAccess.user_id == user["id"])
            ).all()
            == []
        )
    forbidden = client.patch(
        f"/api/v1/users/{user['id']}/fund-access",
        json={"fund_access_mode": "selected", "fund_ids": [], "reason": "restrict"},
    )
    assert forbidden.status_code == 400


def test_helpers_deny_ungranted_products_and_empty_scope(
    funds: list[int], app_and_engine: tuple[object, object]
) -> None:
    with Session(app_and_engine[1]) as session:
        service = AuthService(session)
        admin = service.initialize_admin("admin", "correct horse").user
        user = service.create_user(
            admin,
            "member",
            "correct horse",
            UserRole.OPERATOR,
            fund_access_mode=FundAccessMode.SELECTED,
            fund_ids=[funds[0]],
        )
        assert visible_fund_ids(session, admin) is None
        assert visible_fund_ids(session, user) == frozenset([funds[0]])
        require_fund_access(session, user, funds[0])
        for fund_id in (funds[1], 99999):
            with pytest.raises(HTTPException) as denied:
                require_fund_access(session, user, fund_id)
            assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as shared:
            require_unrestricted_access(session, user)
        assert shared.value.status_code == 403
        service.set_fund_access(
            admin, user.id, FundAccessMode.SELECTED, [], reason="remove grants"
        )
        assert visible_fund_ids(session, user) == frozenset()
        service.set_fund_access(
            admin, user.id, FundAccessMode.ALL, [], reason="grant all"
        )
        assert visible_fund_ids(session, user) is None


def test_listing_scoped_users_loads_grants_in_one_query(
    client: TestClient, funds: list[int], app_and_engine: tuple[object, object]
) -> None:
    initialize(client)
    with Session(app_and_engine[1]) as session:
        admin = session.get(User, 1)
        assert admin is not None
        for number in range(4):
            AuthService(session).create_user(
                admin,
                f"member{number}",
                "correct horse",
                UserRole.VIEWER,
                fund_access_mode=FundAccessMode.SELECTED,
                fund_ids=[funds[0]],
            )
        session.commit()
    statements: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    event.listen(app_and_engine[1], "before_cursor_execute", record)
    try:
        response = client.get("/api/v1/users")
    finally:
        event.remove(app_and_engine[1], "before_cursor_execute", record)
    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 5
    assert (
        len(
            [
                statement
                for statement in statements
                if "FROM user_fund_access" in statement
            ]
        )
        == 1
    )


def test_fund_removal_clears_grants_without_widening_account_scope(
    funds: list[int], app_and_engine: tuple[object, object]
) -> None:
    with Session(app_and_engine[1]) as session:
        service = AuthService(session)
        admin = service.initialize_admin("admin", "correct horse").user
        user = service.create_user(
            admin,
            "member",
            "correct horse",
            UserRole.VIEWER,
            fund_access_mode=FundAccessMode.SELECTED,
            fund_ids=[funds[0]],
        )
        impact = remove_fund(session, funds[0])
        assert impact is not None
        assert impact["removed"]["user_fund_access"] == 1
        session.commit()
        assert visible_fund_ids(session, user) == frozenset()
        assert user.fund_access_mode == "selected"
