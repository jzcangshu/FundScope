from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_db
from app.db.base import Base, FundStatus, SourceType, ValidationLevel, ValuationStatus
from app.db.models import (
    AccountSubjectDaily,
    AuditLog,
    Fund,
    FundDailySnapshot,
    ImportBatch,
    PositionDaily,
    ShareClass,
    ShareClassDailySnapshot,
    User,
    ValidationResult,
    ValuationVersion,
)
from app.db.session import create_engine
from app.main import create_app
from app.publishing import PublishingService


@pytest.fixture()
def app_and_engine(tmp_path) -> Iterator[tuple[FastAPI, object]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app()
    app.state.settings = replace(
        app.state.settings,
        environment="test",
        upload_temp_dir=str(tmp_path / "temp"),
        source_storage_dir=str(tmp_path / "source"),
        max_upload_bytes=1024 * 1024,
    )

    def override_get_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app, engine
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def admin_client(app_and_engine: tuple[FastAPI, object]) -> Iterator[TestClient]:
    app, _ = app_and_engine
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/initialize",
            json={"username": "admin", "password": "correct horse"},
        )
        assert response.status_code == 201
        yield client


def _published_fund(
    engine: object,
    *,
    name: str = "梦溪一号",
    valuation_date: date = date(2026, 8, 25),
    status: FundStatus = FundStatus.ACTIVE,
) -> tuple[int, int]:
    with Session(engine) as session:
        actor = session.scalar(select(User).where(User.username == "admin"))
        assert actor is not None
        fund = Fund(standard_name=name, product_code=f"CODE-{name}", status=status)
        session.add(fund)
        session.flush()
        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=valuation_date,
            version_no=1,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(version)
        session.flush()
        session.add(
            FundDailySnapshot(
                valuation_version_id=version.id,
                total_assets=Decimal(100000),
                total_liabilities=Decimal(10000),
                net_asset_value=Decimal(90000),
                unit_nav=Decimal("1.25"),
                cumulative_unit_nav=Decimal("1.25"),
                cumulative_payout=Decimal(0),
                daily_return=Decimal("0.01"),
                cumulative_return=Decimal("0.25"),
            )
        )
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="export_test",
                level=ValidationLevel.INFO,
                message="通过",
            )
        )
        session.add(
            PositionDaily(
                valuation_version_id=version.id,
                security_code="000001",
                security_name="=危险文本",
                market="A股",
                account="主账户",
                quantity=Decimal(10),
                unit_cost=Decimal(100),
                cost=Decimal(1000),
                market_price=Decimal(110),
                market_value=Decimal(1100),
                nav_weight=Decimal("0.012"),
                valuation_gain=Decimal(100),
            )
        )
        share_class = ShareClass(fund_id=fund.id, share_code="A", share_name="A类")
        session.add(share_class)
        session.flush()
        session.add(
            ShareClassDailySnapshot(
                valuation_version_id=version.id,
                share_class_id=share_class.id,
                net_assets=Decimal(90000),
                paid_in_capital=Decimal(80000),
                unit_nav=Decimal("1.25"),
                cumulative_unit_nav=Decimal("1.25"),
                daily_return=Decimal("0.01"),
            )
        )
        session.add_all(
            [
                AccountSubjectDaily(
                    valuation_version_id=version.id,
                    raw_subject_name="股票",
                    standard_category="权益",
                    is_leaf=True,
                    include_in_holdings=True,
                    market_value=Decimal(30000),
                    market_value_weight=Decimal("0.3333333333"),
                ),
                AccountSubjectDaily(
                    valuation_version_id=version.id,
                    raw_subject_name="现金",
                    standard_category="现金",
                    is_leaf=True,
                    include_in_holdings=True,
                    market_value=Decimal(10000),
                    market_value_weight=Decimal("0.1111111111"),
                ),
            ]
        )
        session.commit()
        PublishingService(session).publish_version(
            version.id,
            actor_user_id=actor.id,
            actor_label=actor.username,
            reason="导出测试发布",
        )
        session.commit()
        return fund.id, version.id


def _csv_rows(response) -> list[list[str]]:
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))


def _role_client(admin_client: TestClient, role: str) -> TestClient:
    created = admin_client.post(
        "/api/v1/users",
        json={
            "username": role,
            "password": "correct horse",
            "role": role,
        },
    )
    assert created.status_code == 201
    client = TestClient(admin_client.app)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": role, "password": "correct horse"},
    )
    assert login.status_code == 200
    return client


def test_exports_are_authenticated_and_viewer_can_read_published_csv(
    admin_client, app_and_engine
) -> None:
    _fund_id, _ = _published_fund(app_and_engine[1])

    unauthenticated = TestClient(admin_client.app).get("/api/v1/exports/overview")
    viewer = _role_client(admin_client, "viewer")
    overview = viewer.get("/api/v1/exports/overview")
    report = viewer.get("/api/v1/exports/imports")

    assert unauthenticated.status_code == 401
    assert overview.status_code == 200
    assert overview.headers["content-type"].startswith("text/csv")
    assert _csv_rows(overview)[1][0] == "CODE-梦溪一号"
    assert report.status_code == 403


def test_overview_and_fund_exports_filter_published_active_data_and_audit(
    admin_client, app_and_engine
) -> None:
    fund_id, _ = _published_fund(app_and_engine[1])
    _published_fund(app_and_engine[1], name="停用产品", status=FundStatus.INACTIVE)

    overview = admin_client.get(
        "/api/v1/exports/overview", params={"as_of": "2026-08-25"}
    )
    funds = admin_client.get(
        "/api/v1/exports/funds", params={"q": "梦", "as_of": "2026-08-25"}
    )
    detail = admin_client.get(f"/api/v1/exports/funds/{fund_id}/overview")

    assert overview.status_code == funds.status_code == detail.status_code == 200
    assert overview.headers["x-data-as-of"] == "2026-08-25"
    assert _csv_rows(overview)[0] == [
        "产品编号",
        "产品名称",
        "估值日",
        "净资产",
        "单位净值",
        "日收益",
        "区间起点",
        "区间当前回撤",
        "区间最大回撤",
    ]
    assert len(_csv_rows(overview)) == 2
    assert len(_csv_rows(funds)) == 2
    assert _csv_rows(detail)[1][1] == "梦溪一号"
    assert _csv_rows(detail)[1][2] == "2026-08-25"

    with Session(app_and_engine[1]) as session:
        actions = session.scalars(
            select(AuditLog.action).where(AuditLog.action.like("export.%"))
        ).all()
    assert actions.count("export.overview") == 1
    assert actions.count("export.funds") == 1
    assert actions.count("export.fund_overview") == 1


def test_detail_exports_cover_nav_allocation_positions_and_shares(
    admin_client, app_and_engine
) -> None:
    fund_id, _ = _published_fund(app_and_engine[1])

    nav = admin_client.get(f"/api/v1/exports/funds/{fund_id}/nav-series")
    allocation = admin_client.get(f"/api/v1/exports/funds/{fund_id}/allocation")
    positions = admin_client.get(f"/api/v1/exports/funds/{fund_id}/positions")
    shares = admin_client.get(f"/api/v1/exports/funds/{fund_id}/share-classes")

    assert [
        _csv_rows(nav)[0][0],
        _csv_rows(allocation)[0][0],
        _csv_rows(positions)[0][0],
        _csv_rows(shares)[0][0],
    ] == [
        "日期",
        "资产类别",
        "证券代码",
        "份额代码",
    ]
    assert _csv_rows(nav)[1][0] == "2026-08-25"
    assert _csv_rows(allocation)[1][0] == "权益"
    assert _csv_rows(positions)[1][1] == "'=危险文本"
    assert _csv_rows(shares)[1][0] == "A"


def test_exports_support_empty_results_and_import_report_for_operator(
    admin_client, app_and_engine
) -> None:
    _published_fund(app_and_engine[1])
    empty = admin_client.get(
        "/api/v1/exports/funds/999999/nav-series", params={"start": "2030-01-01"}
    )
    with Session(app_and_engine[1]) as session:
        session.add(ImportBatch(source_type=SourceType.UPLOAD, file_count=0))
        session.commit()

    operator = _role_client(admin_client, "operator")
    report = operator.get("/api/v1/exports/imports")

    assert empty.status_code == 404
    assert report.status_code == 200
    assert _csv_rows(report)[0] == [
        "批次编号",
        "来源",
        "文件数",
        "批次状态",
        "任务状态",
        "创建时间",
    ]
    assert len(_csv_rows(report)) == 2
