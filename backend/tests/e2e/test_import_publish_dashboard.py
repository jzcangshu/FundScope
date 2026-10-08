from __future__ import annotations

from datetime import date
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.db.base import ValuationStatus
from app.db.models import Fund, ValuationVersion
from app.imports.tasks import process_next_job


def _valuation_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "估值表"
    sheet.append(["证券投资基金估值表"])
    sheet.append(["千帆一号___专用表"])
    sheet.append(["估值日期：2026-08-25"])
    sheet.append(
        [
            "科目代码",
            "科目名称",
            "数量",
            "单位成本",
            "成本",
            "成本占净值%",
            "市价",
            "市值",
            "市值占净值%",
            "估值增值",
            "停牌信息",
        ]
    )
    sheet.append(["10020101", "测试证券", 10, 10, 100, 100, 10, 100, 100, 0, ""])
    sheet.append(["资产类合计", "", "", "", "", "", "", 100, "", "", ""])
    sheet.append(["负债类合计", "", "", "", "", "", "", 0, "", "", ""])
    sheet.append(["基金资产净值", "", "", "", "", "", "", 100, "", "", ""])
    sheet.append(["基金资产净值:A类", "", "", "", "", "", "", 100, "", "", ""])
    sheet.append(["基金单位净值", 1])
    sheet.append(["基金单位净值:A类", 1])
    sheet.append(["累计单位净值", 1])
    sheet.append(["累计单位净值:A类", 1])
    sheet.append(["昨日单位净值", 0.99])
    sheet.append(["净值日增长率(%)", 1.010101])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_upload_worker_publish_and_dashboard_read(admin_client, app_and_engine) -> None:
    admin_client.post(
        "/api/v1/funds",
        json={
            "standard_name": "千帆一号",
            "product_code": "QJ-001",
            "establishment_date": "2024-01-01",
            "aliases": [{"alias": "千帆一号___专用表"}],
        },
    )
    batch_response = admin_client.post(
        "/api/v1/imports", json={"source_type": "upload"}
    )
    assert batch_response.status_code == 201
    batch_id = batch_response.json()["data"]["id"]
    uploaded = admin_client.post(
        f"/api/v1/imports/{batch_id}/files",
        files={
            "file": (
                "千帆一号.xlsx",
                _valuation_xlsx(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert uploaded.status_code == 201
    assert admin_client.post(f"/api/v1/imports/{batch_id}/complete").status_code == 200

    with Session(app_and_engine[1]) as session:
        result = process_next_job(session, admin_client.app.state.settings)
        assert result is not None
        assert result[1] is not None
        assert result[1].published_files == 1
        version = session.query(ValuationVersion).one()
        assert version.status == ValuationStatus.PUBLISHED
    with Session(app_and_engine[1]) as session:
        analysis_result = process_next_job(session, admin_client.app.state.settings)
        assert analysis_result is not None
    overview = admin_client.get("/api/v1/dashboard/overview")
    assert overview.status_code == 200
    assert overview.json()["data"]["fund_count"] == 1
    assert overview.json()["data"]["funds"][0]["name"] == "千帆一号"
    assert overview.json()["data"]["funds"][0]["analysis_status"] == "ready"
    assert overview.json()["meta"]["analysis_status"] == "ready"


def test_viewer_cannot_write_or_publish(admin_client, app_and_engine) -> None:
    created = admin_client.post(
        "/api/v1/users",
        json={"username": "viewer", "password": "correct horse", "role": "viewer"},
    )
    assert created.status_code == 201
    with TestClient(admin_client.app) as viewer:
        viewer.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "correct horse"},
        )

        assert (
            viewer.post("/api/v1/funds", json={"standard_name": "禁止产品"}).status_code
            == 403
        )
        assert (
            viewer.post("/api/v1/imports", json={"source_type": "upload"}).status_code
            == 403
        )

    with Session(app_and_engine[1]) as session:
        actor = session.query(Fund).first()
        if actor is None:
            actor = Fund(standard_name="已有产品")
            session.add(actor)
            session.flush()
        version = ValuationVersion(
            fund_id=actor.id,
            valuation_date=date(2026, 8, 25),
            version_no=1,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(version)
        session.commit()
        version_id = version.id
    with TestClient(admin_client.app) as viewer:
        viewer.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "correct horse"},
        )
        assert (
            viewer.post(f"/api/v1/valuations/{version_id}/publish", json={}).status_code
            == 403
        )
