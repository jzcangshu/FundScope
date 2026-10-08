from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog


def _role_client(admin_client: TestClient, role: str) -> TestClient:
    created = admin_client.post(
        "/api/v1/users",
        json={
            "username": f"{role}-catalog",
            "password": "correct horse",
            "role": role,
        },
    )
    assert created.status_code == 201
    client = TestClient(admin_client.app)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": f"{role}-catalog", "password": "correct horse"},
    )
    assert login.status_code == 200
    return client


def test_fund_crud_lifecycle_alias_conflict_and_audit(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    created = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "  梦溪一号  ",
            "product_code": "  M-001 ",
            "establishment_date": "2024-06-24",
            "strategy": "股票多头",
            "aliases": [{"alias": "  梦溪一号专用表  "}],
        },
    )

    assert created.status_code == 201
    fund = created.json()["data"]
    assert fund["standard_name"] == "梦溪一号"
    assert fund["product_code"] == "M-001"
    assert fund["establishment_date"] == "2024-06-24"
    fund_id = fund["id"]

    aliases = admin_client.get(f"/api/v1/funds/{fund_id}/aliases")
    assert aliases.status_code == 200
    alias = aliases.json()["data"][0]
    assert alias["alias"] == "梦溪一号专用表"

    duplicate_alias = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "千帆一号",
            "product_code": "QJ-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": " 梦溪一号专用表 "}],
        },
    )
    assert duplicate_alias.status_code == 409
    assert "UNIQUE" not in duplicate_alias.text.upper()

    duplicate_name = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": " 梦溪一号 ",
            "product_code": "M-002",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "梦溪一号新别名"}],
        },
    )
    duplicate_code = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "天衡上将",
            "product_code": "m-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "天衡上将估值表"}],
        },
    )
    assert duplicate_name.status_code == 409
    assert duplicate_code.status_code == 409

    updated = admin_client.patch(
        f"/api/v1/funds/{fund_id}", json={"manager": "新负责人", "notes": "已核对"}
    )
    unknown = admin_client.patch(
        f"/api/v1/funds/{fund_id}", json={"unexpected": "拒绝"}
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["manager"] == "新负责人"
    assert unknown.status_code == 422

    disabled_without_reason = admin_client.post(f"/api/v1/funds/{fund_id}/disable")
    disabled = admin_client.post(
        f"/api/v1/funds/{fund_id}/disable", json={"reason": "产品清算"}
    )
    enabled = admin_client.post(f"/api/v1/funds/{fund_id}/enable")
    assert disabled_without_reason.status_code == 422
    assert disabled.json()["data"]["status"] == "inactive"
    assert enabled.json()["data"]["status"] == "active"

    patched_alias = admin_client.patch(
        f"/api/v1/funds/{fund_id}/aliases/{alias['id']}",
        json={"source_location": "历史目录", "match_priority": 10},
    )
    deleted_alias = admin_client.delete(
        f"/api/v1/funds/{fund_id}/aliases/{alias['id']}"
    )
    assert patched_alias.status_code == 200
    assert patched_alias.json()["data"]["match_priority"] == 10
    assert deleted_alias.status_code == 200
    assert deleted_alias.json()["data"]["deleted"] is True

    with Session(app_and_engine[1]) as session:
        actions = session.scalars(
            select(AuditLog.action).where(
                AuditLog.resource_type.in_(["fund", "fund_alias"])
            )
        ).all()
    assert {
        "fund.create",
        "fund.update",
        "fund.disable",
        "fund.enable",
        "fund_alias.create",
        "fund_alias.update",
        "fund_alias.delete",
    }.issubset(set(actions))


def test_fund_can_be_renamed_to_its_own_alias(
    admin_client: TestClient,
) -> None:
    created = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "原产品名",
            "product_code": "RENAME-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "目标产品名"}],
        },
    )
    fund_id = created.json()["data"]["id"]

    response = admin_client.patch(
        f"/api/v1/funds/{fund_id}", json={"standard_name": "目标产品名"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["standard_name"] == "目标产品名"


def test_fund_create_requires_identity_fields_and_alias(
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        "/api/v1/funds", json={"standard_name": "缺少身份信息"}
    )

    assert response.status_code == 422


def test_fund_detail_returns_aliases_and_share_classes(
    admin_client: TestClient,
) -> None:
    created = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "详情产品",
            "product_code": "DETAIL-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "详情产品估值表"}],
        },
    )
    fund_id = created.json()["data"]["id"]
    share_class = admin_client.post(
        f"/api/v1/funds/{fund_id}/share-classes",
        json={"share_code": "A", "share_name": "A类"},
    )

    detail = admin_client.get(f"/api/v1/funds/{fund_id}")

    assert created.status_code == 201
    assert share_class.status_code == 201
    assert detail.status_code == 200
    assert detail.json()["data"]["aliases"][0]["alias"] == "详情产品估值表"
    assert detail.json()["data"]["share_classes"][0]["share_code"] == "A"


def test_catalog_permissions_allow_operator_and_reject_viewer(
    admin_client: TestClient,
) -> None:
    operator = _role_client(admin_client, "operator")
    viewer = _role_client(admin_client, "viewer")

    operator_created = operator.post(
        "/api/v1/funds",
        json={
            "standard_name": "业务员创建产品",
            "product_code": "OP-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "业务员估值表"}],
        },
    )
    viewer_created = viewer.post(
        "/api/v1/funds", json={"standard_name": "看板不应创建产品"}
    )
    viewer_mapping = viewer.get("/api/v1/subjects/mappings")

    assert operator_created.status_code == 201
    assert viewer_created.status_code == 403
    assert viewer_mapping.status_code == 403
    assert viewer.get("/api/v1/dashboard/overview").status_code == 200


def test_share_class_lifecycle_and_snapshot_safe_fields(
    admin_client: TestClient,
) -> None:
    fund = admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "份额测试产品",
            "product_code": "SHARE-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "份额测试估值表"}],
        },
    ).json()["data"]
    fund_id = fund["id"]

    created = admin_client.post(
        f"/api/v1/funds/{fund_id}/share-classes",
        json={
            "share_code": " A ",
            "share_name": " A类 ",
            "enabled_from": "2024-01-01",
        },
    )
    duplicate = admin_client.post(
        f"/api/v1/funds/{fund_id}/share-classes",
        json={"share_code": "a", "share_name": "重复"},
    )
    assert created.status_code == 201
    assert created.json()["data"]["share_code"] == "A"
    assert duplicate.status_code == 409
    share_id = created.json()["data"]["id"]

    disabled = admin_client.post(
        f"/api/v1/funds/{fund_id}/share-classes/{share_id}/disable",
        json={"reason": "份额终止", "disabled_from": "2026-08-25"},
    )
    inactive = admin_client.get(
        f"/api/v1/funds/{fund_id}/share-classes", params={"status": "inactive"}
    )
    enabled = admin_client.post(
        f"/api/v1/funds/{fund_id}/share-classes/{share_id}/enable"
    )
    patched = admin_client.patch(
        f"/api/v1/funds/{fund_id}/share-classes/{share_id}",
        json={"share_name": "A类修订"},
    )

    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "inactive"
    assert inactive.json()["meta"]["total"] == 1
    assert enabled.json()["data"]["status"] == "active"
    assert patched.json()["data"]["share_name"] == "A类修订"


def test_subject_mapping_validation_filters_pagination_disable_and_audit(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    missing_matcher = admin_client.post(
        "/api/v1/subjects/mappings",
        json={"standard_category": "股票", "rule_version": "v1"},
    )
    invalid_dates = admin_client.post(
        "/api/v1/subjects/mappings",
        json={
            "subject_code_or_prefix": "1002",
            "standard_category": "股票",
            "rule_version": "v1",
            "valid_from": "2026-08-25",
            "valid_to": "2026-08-24",
        },
    )
    assert missing_matcher.status_code == 422
    assert invalid_dates.status_code == 422

    created = []
    for code, category in (("1002", "股票"), ("1003", "债券"), (None, "股票")):
        payload = {
            "raw_name_pattern": "现金" if code is None else None,
            "subject_code_or_prefix": code,
            "standard_category": category,
            "include_in_holdings": code == "1002",
            "rule_version": "v1",
        }
        response = admin_client.post("/api/v1/subjects/mappings", json=payload)
        assert response.status_code == 201
        created.append(response.json()["data"])

    page = admin_client.get(
        "/api/v1/subjects/mappings",
        params={"category": "股票", "page": 1, "page_size": 1},
    )
    unknown = admin_client.patch(
        f"/api/v1/subjects/mappings/{created[0]['id']}",
        json={"not_allowed": True},
    )
    invalid_patch = admin_client.patch(
        f"/api/v1/subjects/mappings/{created[0]['id']}",
        json={"valid_from": "2026-08-25", "valid_to": "2026-08-24"},
    )
    disabled = admin_client.post(
        f"/api/v1/subjects/mappings/{created[0]['id']}/disable",
        json={"reason": "规则修订"},
    )
    inactive = admin_client.get(
        "/api/v1/subjects/mappings", params={"status": "inactive"}
    )

    assert page.status_code == 200
    assert page.json()["meta"] == {"page": 1, "page_size": 1, "total": 2}
    assert unknown.status_code == 422
    assert invalid_patch.status_code == 422
    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "inactive"
    assert inactive.json()["meta"]["total"] == 1

    with Session(app_and_engine[1]) as session:
        actions = session.scalars(
            select(AuditLog.action).where(AuditLog.resource_type == "subject_mapping")
        ).all()
    assert "subject_mapping.create" in actions
    assert "subject_mapping.disable" in actions


def test_mapping_disable_accepts_empty_body(admin_client: TestClient) -> None:
    mapping = admin_client.post(
        "/api/v1/subjects/mappings",
        json={
            "subject_code_or_prefix": "2001",
            "standard_category": "现金",
            "rule_version": "v1",
        },
    ).json()["data"]
    response = admin_client.post(f"/api/v1/subjects/mappings/{mapping['id']}/disable")
    assert response.status_code == 200
