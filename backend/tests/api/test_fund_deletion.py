"""Fund deletion: ordering, guards, and the two history-preserving exceptions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import (
    AnalysisRunStatus,
    FundStatus,
    RiskSeverity,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    AuditLog,
    CompanyMetricDaily,
    Fund,
    FundAlias,
    FundDailySnapshot,
    FundMetricDaily,
    RiskEvent,
    RiskRule,
    ShareClass,
    ShareClassDailySnapshot,
    ValuationVersion,
)

VALUATION_DATE = date(2026, 8, 25)


def _seed_fund_with_history(engine: object, *, status: FundStatus) -> int:
    """Build one fund carrying the full dependency fan-out deletion must clear."""

    with Session(engine) as session:  # type: ignore[arg-type]
        fund = Fund(standard_name="待删除产品", status=status)
        session.add(fund)
        session.flush()

        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=VALUATION_DATE,
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
                previous_unit_nav=Decimal("1.24"),
                daily_return=Decimal("0.01"),
            )
        )

        share_class = ShareClass(fund_id=fund.id, share_code="A", share_name="A 类份额")
        session.add(share_class)
        session.flush()
        session.add(
            ShareClassDailySnapshot(
                valuation_version_id=version.id, share_class_id=share_class.id
            )
        )
        session.add(
            FundAlias(fund_id=fund.id, alias="待删除产品专用表", match_priority=0)
        )

        run = AnalysisRun(
            trigger_version_id=version.id,
            trigger_reason="test",
            methodology_version="v1",
            status=AnalysisRunStatus.SUCCEEDED,
        )
        session.add(run)
        session.flush()
        session.add(
            FundMetricDaily(
                fund_id=fund.id,
                valuation_date=VALUATION_DATE,
                source_analysis_run_id=run.id,
            )
        )
        session.add(
            CompanyMetricDaily(
                valuation_date=VALUATION_DATE,
                source_analysis_run_id=run.id,
                effective_fund_count=1,
            )
        )

        rule = RiskRule(
            rule_code="test_rule",
            rule_type="drawdown",
            scope="fund",
            threshold=Decimal("0.2"),
            severity=RiskSeverity.INFO,
            version="v1",
            enabled=True,
        )
        session.add(rule)
        session.flush()
        session.add(
            RiskEvent(
                risk_rule_id=rule.id,
                fund_id=fund.id,
                valuation_date=VALUATION_DATE,
                severity=RiskSeverity.INFO,
                first_triggered_at=datetime.now(UTC),
                last_triggered_at=datetime.now(UTC),
            )
        )
        session.commit()
        return fund.id


def _operator_client(admin_client: TestClient) -> TestClient:
    created = admin_client.post(
        "/api/v1/users",
        json={
            "username": "operator-delete",
            "password": "correct horse",
            "role": "operator",
        },
    )
    assert created.status_code == 201
    client = TestClient(admin_client.app)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operator-delete", "password": "correct horse"},
    )
    assert login.status_code == 200
    return client


def _count(session: Session, model: object) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)  # type: ignore[arg-type]


def test_deletion_impact_reports_rows_without_deleting(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    _app, engine = app_and_engine
    fund_id = _seed_fund_with_history(engine, status=FundStatus.INACTIVE)

    response = admin_client.get(f"/api/v1/funds/{fund_id}/deletion-impact")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["fund"]["standard_name"] == "待删除产品"
    assert payload["removed"]["valuation_version"] == 1
    assert payload["removed"]["fund_daily_snapshot"] == 1
    assert payload["removed"]["share_class_daily_snapshot"] == 1
    assert payload["removed"]["fund_metric_daily"] == 1
    assert payload["removed"]["fund_alias"] == 1
    assert payload["removed"]["share_class"] == 1
    assert payload["removed"]["risk_event"] == 1
    assert payload["detached_analysis_runs"] == 1
    assert payload["preserved_company_metrics"] == 1

    with Session(engine) as session:  # type: ignore[arg-type]
        assert session.get(Fund, fund_id) is not None


def test_delete_fund_clears_children_detaches_runs_and_keeps_company_history(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    _app, engine = app_and_engine
    fund_id = _seed_fund_with_history(engine, status=FundStatus.INACTIVE)

    response = admin_client.request(
        "DELETE", f"/api/v1/funds/{fund_id}", json={"reason": "产品已清算，归档后移除"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["deleted"] is True

    with Session(engine) as session:  # type: ignore[arg-type]
        assert session.get(Fund, fund_id) is None
        assert _count(session, ValuationVersion) == 0
        assert _count(session, FundDailySnapshot) == 0
        assert _count(session, ShareClassDailySnapshot) == 0
        assert _count(session, ShareClass) == 0
        assert _count(session, FundAlias) == 0
        assert _count(session, FundMetricDaily) == 0
        assert _count(session, RiskEvent) == 0
        # The run survives with its trigger cleared, so company history stays.
        assert _count(session, AnalysisRun) == 1
        assert _count(session, CompanyMetricDaily) == 1
        run = session.scalar(select(AnalysisRun))
        assert run is not None and run.trigger_version_id is None

        audit = session.scalar(select(AuditLog).where(AuditLog.action == "fund.delete"))
        assert audit is not None
        assert audit.reason == "产品已清算，归档后移除"
        assert audit.summary is not None
        assert (
            audit.summary["total_removed"] == response.json()["data"]["total_removed"]
        )


def test_delete_fund_requires_the_fund_to_be_disabled_first(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    _app, engine = app_and_engine
    fund_id = _seed_fund_with_history(engine, status=FundStatus.ACTIVE)

    response = admin_client.request(
        "DELETE", f"/api/v1/funds/{fund_id}", json={"reason": "直接删除在用的产品"}
    )
    assert response.status_code == 409

    with Session(engine) as session:  # type: ignore[arg-type]
        assert session.get(Fund, fund_id) is not None
        assert _count(session, ValuationVersion) == 1


def test_delete_fund_is_admin_only(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    _app, engine = app_and_engine
    fund_id = _seed_fund_with_history(engine, status=FundStatus.INACTIVE)
    operator = _operator_client(admin_client)

    response = operator.request(
        "DELETE", f"/api/v1/funds/{fund_id}", json={"reason": "运营尝试删除"}
    )
    assert response.status_code == 403

    with Session(engine) as session:  # type: ignore[arg-type]
        assert session.get(Fund, fund_id) is not None
        assert _count(session, ValuationVersion) == 1
