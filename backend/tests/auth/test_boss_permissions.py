"""Boss account grants are separate from account administration and data reads."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Fund

from .test_fund_access import create_member, initialize

pytestmark = pytest.mark.security


def login_as(admin: TestClient, role: str, **fields: object) -> tuple[TestClient, int]:
    account = create_member(admin, username=role, role=role, **fields)
    client = TestClient(admin.app)
    response = client.post(
        "/api/v1/auth/login", json={"username": role, "password": "correct horse"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["role"] == role
    return client, account["id"]


@pytest.mark.parametrize("role", ["admin", "boss", "viewer", "operator", None])
def test_only_boss_and_admin_can_read_permissions(client, role):
    initialize(client)
    if role == "admin":
        reader = client
    elif role is None:
        reader = TestClient(client.app)
    else:
        reader, _ = login_as(client, role)
    for path in ("/api/v1/users/permissions", "/api/v1/users/permissions/funds"):
        response = reader.get(path)
        assert response.status_code == (
            401 if role is None else 200 if role in {"admin", "boss"} else 403
        )


def test_permission_overview_lists_full_grants_without_expanding_boss_data_access(
    client, app_and_engine
):
    initialize(client)
    with Session(app_and_engine[1]) as session:
        funds = [
            Fund(standard_name="授权产品", product_code="P1"),
            Fund(standard_name="其他产品", product_code="P2", status="inactive"),
        ]
        session.add_all(funds)
        session.commit()
        ids = [fund.id for fund in funds]
    boss, _ = login_as(client, "boss", fund_access_mode="selected", fund_ids=[ids[0]])
    target = create_member(
        client,
        display_name="指定产品员工",
        fund_access_mode="selected",
        fund_ids=[ids[1]],
    )
    response = boss.get(
        "/api/v1/users/permissions",
        params={
            "q": "指定产品",
            "role": "viewer",
            "status": "active",
            "page_size": 1,
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    payload = response.json()
    assert payload["meta"] == {"page": 1, "page_size": 1, "total": 1}
    account = payload["data"][0]
    assert set(account) == {
        "id",
        "username",
        "display_name",
        "role",
        "status",
        "last_login_at",
        "navigation",
        "fund_access_mode",
        "fund_ids",
        "funds",
    }
    assert account["funds"] == [
        {"id": ids[1], "name": "其他产品", "product_code": "P2", "status": "inactive"}
    ]
    assert boss.get(f"/api/v1/funds/{ids[1]}").status_code == 404
    assert [fund["id"] for fund in boss.get("/api/v1/funds").json()["data"]] == [ids[0]]
    assert (
        boss.get("/api/v1/users/permissions", params={"q": "%"}).json()["meta"]["total"]
        == 0
    )
    assert (
        boss.get("/api/v1/users/permissions", params={"q": "admin"}).json()["data"]
        == []
    )
    first = boss.get("/api/v1/users/permissions?page_size=1").json()
    second = boss.get("/api/v1/users/permissions?page_size=1&page=2").json()
    assert first["meta"]["total"] == second["meta"]["total"] == 1
    assert first["data"][0]["id"] == target["id"]
    assert second["data"] == []
    assert boss.get("/api/v1/users/permissions?page=99").json()["data"] == []
    changed = client.patch(
        f"/api/v1/users/{target['id']}/fund-access",
        json={"fund_access_mode": "selected", "fund_ids": [], "reason": "收回产品"},
    )
    assert changed.status_code == 200
    assert (
        boss.get("/api/v1/users/permissions?q=member").json()["data"][0]["funds"] == []
    )


@pytest.mark.parametrize("mode", ["all", "selected"])
def test_boss_overview_hides_test_accounts_before_search_and_pagination(client, mode):
    initialize(client)
    boss, _ = login_as(client, "boss", fund_access_mode=mode, fund_ids=[])
    operator = create_member(
        client, username="operator", role="operator", display_name="测试业务账号"
    )
    client.post(f"/api/v1/users/{operator['id']}/disable")
    # Real accounts remain visible, including matching roles, name fragments and
    # a display name identical to a hidden username.
    for username, role, display_name in (
        ("amy_ops", "operator", "boss"),
        ("bob_e", "boss", "Sam"),
        ("carol_admin", "admin", "运维管理员"),
    ):
        create_member(client, username=username, role=role, display_name=display_name)

    path = "/api/v1/users/permissions"
    for page, usernames in ((1, ["amy_ops", "bob_e"]), (2, ["carol_admin"]), (3, [])):
        response = boss.get(path, params={"page": page, "page_size": 2})
        assert response.status_code == 200
        payload = response.json()
        assert payload["meta"] == {"page": page, "page_size": 2, "total": 3}
        assert [row["username"] for row in payload["data"]] == usernames

    for params, usernames in (
        ({"q": "admin"}, ["carol_admin"]),
        ({"q": "boss"}, ["amy_ops"]),
        ({"q": "operator"}, []),
        ({"q": "测试业务账号"}, []),
        ({"role": "boss"}, ["bob_e"]),
        ({"role": "admin"}, ["carol_admin"]),
        ({"status": "disabled"}, []),
        ({"q": "boss", "role": "operator", "status": "active"}, ["amy_ops"]),
    ):
        response = boss.get(path, params=params)
        assert response.status_code == 200
        payload = response.json()
        assert payload["meta"]["total"] == len(usernames)
        assert [row["username"] for row in payload["data"]] == usernames

    for admin_path in (path, "/api/v1/users"):
        response = client.get(admin_path)
        assert response.status_code == 200
        payload = response.json()
        assert payload["meta"]["total"] == 6
        assert {row["username"] for row in payload["data"]} == {
            "admin",
            "boss",
            "operator",
            "amy_ops",
            "bob_e",
            "carol_admin",
        }
    assert boss.get("/api/v1/auth/me").json()["data"]["username"] == "boss"


@pytest.mark.parametrize("mode", ["all", "selected"])
def test_boss_overview_hides_disabled_accounts_and_restores_enabled_accounts(
    client, app_and_engine, mode
):
    initialize(client)
    boss, _ = login_as(client, "boss", fund_access_mode=mode, fund_ids=[])
    with Session(app_and_engine[1]) as session:
        fund = Fund(standard_name="保留授权产品")
        session.add(fund)
        session.commit()
        fund_id = fund.id

    disabled = []
    for role in ("admin", "boss", "operator", "viewer"):
        account = create_member(
            client,
            username=f"archived_{role}",
            role=role,
            display_name="已停用员工",
            fund_access_mode="all" if role == "admin" else "selected",
            fund_ids=[] if role == "admin" else [fund_id],
        )
        assert client.post(f"/api/v1/users/{account['id']}/disable").status_code == 200
        disabled.append(account)
    for username in ("amy_ops", "bob_e"):
        create_member(client, username=username)

    path = "/api/v1/users/permissions"
    for page, usernames in ((1, ["amy_ops"]), (2, ["bob_e"]), (3, [])):
        response = boss.get(path, params={"page": page, "page_size": 1})
        assert response.status_code == 200
        payload = response.json()
        assert payload["meta"] == {"page": page, "page_size": 1, "total": 2}
        assert [row["username"] for row in payload["data"]] == usernames

    for params in (
        {"q": "archived"},
        {"q": "已停用员工"},
        {"status": "disabled"},
        {"q": "archived_viewer", "role": "viewer", "status": "disabled"},
        {"role": "operator"},
    ):
        response = boss.get(path, params=params)
        assert response.status_code == 200
        assert response.json()["data"] == []
        assert response.json()["meta"]["total"] == 0
    assert boss.get(path, params={"status": "active"}).json()["meta"]["total"] == 2

    for admin_path in (path, "/api/v1/users"):
        assert client.get(admin_path).json()["meta"]["total"] == 8
        payload = client.get(admin_path, params={"status": "disabled"}).json()
        assert payload["meta"]["total"] == 4
        assert {row["id"] for row in payload["data"]} == {
            account["id"] for account in disabled
        }

    target = disabled[-1]
    assert client.post(f"/api/v1/users/{target['id']}/enable").status_code == 200
    payload = boss.get(path, params={"q": target["username"]}).json()
    assert payload["meta"]["total"] == 1
    restored = payload["data"][0]
    assert restored["id"] == target["id"]
    assert restored["status"] == "active"
    assert restored["fund_ids"] == [fund_id]
    assert restored["funds"][0]["name"] == "保留授权产品"
    assert boss.get(path).json()["meta"]["total"] == 3


def test_boss_has_viewer_capabilities_and_cannot_administer_accounts(client):
    initialize(client)
    boss, boss_id = login_as(client, "boss")
    for path in (
        "/dashboard/overview",
        "/funds",
        "/risk/events",
        "/risk/rules",
        "/exports/overview",
    ):
        assert boss.get(f"/api/v1{path}").status_code == 200
    for path in (
        "/users",
        "/imports",
        "/reviews",
        "/mail/settings",
        "/audit-logs",
        "/system/settings",
        "/subjects/mappings",
    ):
        assert boss.get(f"/api/v1{path}").status_code == 403
    writes = [
        (
            "POST",
            "/users",
            {"username": "forbidden", "password": "correct horse", "role": "admin"},
        ),
        ("PATCH", f"/users/{boss_id}/role", {"role": "admin"}),
        (
            "PATCH",
            f"/users/{boss_id}/fund-access",
            {"fund_access_mode": "all", "fund_ids": [], "reason": "forbidden"},
        ),
        ("POST", f"/users/{boss_id}/disable", {}),
        ("POST", f"/users/{boss_id}/enable", {}),
        (
            "POST",
            f"/users/{boss_id}/reset-password",
            {"password": "replacement password"},
        ),
        ("POST", f"/users/{boss_id}/revoke-sessions", {}),
        ("POST", "/imports", {"source_type": "upload"}),
        ("POST", "/funds", {"standard_name": "forbidden"}),
        ("POST", "/risk/rules/recalculate", {}),
        (
            "POST",
            "/risk/events/1/handle",
            {"status": "acknowledged", "handling_note": "forbidden"},
        ),
    ]
    for method, path, payload in writes:
        response = boss.request(method, f"/api/v1{path}", json=payload)
        assert response.status_code == 403, (path, response.text)
    # Ordinary viewers already have risk-rule configuration; the boss inherits it.
    assert (
        boss.post(
            "/api/v1/risk/rules",
            json={
                "rule_code": "boss_rule",
                "rule_type": "daily_return",
                "threshold": -0.1,
            },
        ).status_code
        == 201
    )
    assert (
        client.patch(
            f"/api/v1/users/{boss_id}/fund-access",
            json={"fund_access_mode": "selected", "fund_ids": [], "reason": "限定范围"},
        ).status_code
        == 200
    )
    assert boss.get("/api/v1/risk/rules").status_code == 403
    assert (
        boss.post(
            "/api/v1/risk/rules",
            json={
                "rule_code": "blocked_rule",
                "rule_type": "daily_return",
                "threshold": -0.1,
            },
        ).status_code
        == 403
    )
    assert boss.get("/api/v1/users/permissions").status_code == 200


def test_role_changes_take_effect_in_existing_session(client):
    initialize(client)
    member, member_id = login_as(
        client, "viewer", fund_access_mode="selected", fund_ids=[]
    )
    assert member.get("/api/v1/users/permissions").status_code == 403
    assert member.get("/api/v1/users/permissions/funds").status_code == 403
    assert (
        member.patch(
            "/api/v1/users/1/fund-access",
            json={"fund_access_mode": "all", "fund_ids": [], "reason": "role revoked"},
        ).status_code
        == 403
    )
    changed = client.patch(f"/api/v1/users/{member_id}/role", json={"role": "boss"})
    assert changed.status_code == 200
    assert changed.json()["data"]["fund_access_mode"] == "selected"
    assert member.get("/api/v1/users/permissions").status_code == 200
    assert member.get("/api/v1/auth/me").json()["data"]["role"] == "boss"
    assert (
        client.patch(
            f"/api/v1/users/{member_id}/role", json={"role": "viewer"}
        ).status_code
        == 200
    )
    assert member.get("/api/v1/users/permissions").status_code == 403
    client.patch(f"/api/v1/users/{member_id}/role", json={"role": "boss"})
    client.post(f"/api/v1/users/{member_id}/disable")
    assert member.get("/api/v1/users/permissions").status_code == 401


@pytest.mark.parametrize("mode", ["all", "selected"])
@pytest.mark.parametrize("target_role", ["viewer", "operator", "boss"])
def test_boss_can_change_other_accounts_scope_with_audit_and_live_effect(
    client, app_and_engine, mode, target_role
):
    initialize(client)
    with Session(app_and_engine[1]) as session:
        funds = [
            Fund(standard_name="产品甲"),
            Fund(standard_name="产品乙", status="inactive"),
        ]
        session.add_all(funds)
        session.commit()
        ids = [fund.id for fund in funds]
    boss, boss_id = login_as(
        client,
        "boss",
        fund_access_mode=mode,
        fund_ids=ids[:1] if mode == "selected" else [],
    )
    target = create_member(
        client, role=target_role, fund_access_mode="selected", fund_ids=ids[:1]
    )
    member = TestClient(client.app)
    assert (
        member.post(
            "/api/v1/auth/login",
            json={"username": "member", "password": "correct horse"},
        ).status_code
        == 200
    )
    path = f"/api/v1/users/{target['id']}/fund-access"
    changed = boss.patch(
        path,
        json={
            "fund_access_mode": "selected",
            "fund_ids": [ids[1], ids[1]],
            "reason": "  调整观看权限  ",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["role"] == target_role
    assert changed.json()["data"]["fund_ids"] == [ids[1]]
    assert member.get("/api/v1/auth/me").json()["data"]["fund_ids"] == [ids[1]]
    assert [row["id"] for row in member.get("/api/v1/funds").json()["data"]] == [ids[1]]
    assert member.get(f"/api/v1/funds/{ids[0]}").status_code == 404
    if mode == "selected":
        assert boss.get(f"/api/v1/funds/{ids[1]}").status_code == 404
    with Session(app_and_engine[1]) as session:
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "user.change_fund_access")
        ).one()
        assert audit.actor_user_id == boss_id
        assert audit.resource_id == str(target["id"])
        assert audit.reason == "调整观看权限"
        assert audit.summary == {
            "before": {"fund_access_mode": "selected", "fund_ids": ids[:1]},
            "after": {"fund_access_mode": "selected", "fund_ids": [ids[1]]},
        }
    for payload, status in [
        (
            {"fund_access_mode": "selected", "fund_ids": [99999], "reason": "unknown"},
            400,
        ),
        ({"fund_access_mode": "all", "fund_ids": [], "reason": " "}, 422),
        (
            {
                "fund_access_mode": "all",
                "fund_ids": [],
                "reason": "change",
                "role": "admin",
            },
            422,
        ),
    ]:
        assert boss.patch(path, json=payload).status_code == status
        assert member.get("/api/v1/auth/me").json()["data"]["fund_ids"] == [ids[1]]
    assert (
        boss.patch(
            path,
            json={"fund_access_mode": "selected", "fund_ids": [], "reason": "清空"},
        ).status_code
        == 200
    )
    assert member.get("/api/v1/funds").json()["meta"]["total"] == 0
    assert (
        boss.patch(
            path, json={"fund_access_mode": "all", "fund_ids": [], "reason": "全部"}
        ).status_code
        == 200
    )
    assert member.get("/api/v1/funds").json()["meta"]["total"] == 2
    assert member.get("/api/v1/auth/me").json()["data"]["role"] == target_role


def test_boss_cannot_change_self_or_admin_and_demotion_revokes_editing(client):
    initialize(client)
    boss, boss_id = login_as(client, "boss", fund_access_mode="selected", fund_ids=[])
    target = create_member(client)
    payload = {"fund_access_mode": "all", "fund_ids": [], "reason": "test"}
    for target_id in (1, boss_id):
        assert (
            boss.patch(
                f"/api/v1/users/{target_id}/fund-access", json=payload
            ).status_code
            == 403
        )
    assert boss.get("/api/v1/auth/me").json()["data"]["fund_access_mode"] == "selected"
    assert boss.patch("/api/v1/users/9999/fund-access", json=payload).status_code == 404
    client.patch(f"/api/v1/users/{boss_id}/role", json={"role": "viewer"})
    assert (
        boss.patch(
            f"/api/v1/users/{target['id']}/fund-access", json=payload
        ).status_code
        == 403
    )
    assert boss.get("/api/v1/users/permissions/funds").status_code == 403


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_other_roles_cannot_change_another_accounts_scope(client, role):
    initialize(client)
    reader, _ = login_as(client, role)
    target = create_member(client, fund_access_mode="selected", fund_ids=[])
    response = reader.patch(
        f"/api/v1/users/{target['id']}/fund-access",
        json={"fund_access_mode": "all", "fund_ids": [], "reason": "forbidden"},
    )
    assert response.status_code == 403
    assert (
        client.get("/api/v1/users?q=member").json()["data"][0]["fund_access_mode"]
        == "selected"
    )


def test_permission_product_catalog_includes_unassigned_inactive_products_without_data(
    client, app_and_engine
):
    initialize(client)
    with Session(app_and_engine[1]) as session:
        funds = [
            Fund(
                standard_name=f"Product {number:03}",
                product_code=f"P{number:03}",
                status="inactive" if number == 101 else "active",
            )
            for number in range(102)
        ]
        session.add_all(funds)
        session.commit()
        hidden = funds[-1].id
    boss, _ = login_as(client, "boss", fund_access_mode="selected", fund_ids=[])
    first = boss.get("/api/v1/users/permissions/funds").json()
    second = boss.get("/api/v1/users/permissions/funds?page=2").json()
    assert first["meta"] == {"page": 1, "page_size": 100, "total": 102}
    assert len(first["data"]) == 100 and len(second["data"]) == 2
    assert set(second["data"][-1]) == {"id", "name", "product_code", "status"}
    assert second["data"][-1] == {
        "id": hidden,
        "name": "Product 101",
        "product_code": "P101",
        "status": "inactive",
    }
    assert boss.get("/api/v1/funds").json()["meta"]["total"] == 0
    assert boss.get(f"/api/v1/funds/{hidden}").status_code == 404
    assert boss.get(f"/api/v1/exports/funds/{hidden}/nav-series").status_code == 404
