from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import JobStatus, ValuationStatus
from app.db.models import (
    AnalysisRun,
    BackgroundJob,
    Fund,
    FundDailySnapshot,
    ValidationResult,
    ValuationVersion,
)
from app.imports.tasks import process_next_job
from app.publishing import PublishingService


def _drain(client, engine):
    with Session(engine) as session:
        for _ in range(10):
            result = process_next_job(session, client.app.state.settings)
            if result is None:
                return
            assert result[0].status == JobStatus.SUCCEEDED
    raise AssertionError("test queue did not drain")


def _seed_company(client, engine):
    ids = []
    with Session(engine) as session:
        publisher = PublishingService(session)
        for name, last_nav in (("波动产品", "0.8"), ("稳定产品", "1")):
            fund = Fund(standard_name=name)
            session.add(fund)
            session.flush()
            ids.append(fund.id)
            for day, nav in ((date(2026, 8, 24), "1"), (date(2026, 8, 25), last_nav)):
                version = ValuationVersion(
                    fund_id=fund.id,
                    valuation_date=day,
                    version_no=1,
                    status=ValuationStatus.PUBLISHABLE,
                )
                session.add(version)
                session.flush()
                session.add_all(
                    [
                        FundDailySnapshot(
                            valuation_version_id=version.id,
                            net_asset_value=100,
                            unit_nav=Decimal(nav),
                            cumulative_unit_nav=Decimal(nav),
                        ),
                        ValidationResult(
                            valuation_version_id=version.id,
                            rule_code="test",
                            level="info",
                            message="通过",
                        ),
                    ]
                )
                session.flush()
                publisher.publish_version(version.id, actor_user_id=None)
        session.commit()
    _drain(client, engine)
    return ids


def test_disabling_and_enabling_fund_rebuilds_company_history(
    admin_client, app_and_engine
):
    engine = app_and_engine[1]
    volatile_id, _ = _seed_company(admin_client, engine)
    baseline = admin_client.get("/api/v1/dashboard/overview").json()
    assert baseline["meta"]["analysis_status"] == "ready"
    assert Decimal(baseline["data"]["company_index"]) == Decimal("0.9")

    assert (
        admin_client.post(
            f"/api/v1/funds/{volatile_id}/disable", json={"reason": "调整范围"}
        ).status_code
        == 200
    )
    pending = admin_client.get("/api/v1/dashboard/overview").json()
    assert pending["meta"]["analysis_status"] == "pending"
    assert pending["data"]["company_index"] is None
    _drain(admin_client, engine)

    for params in ({}, {"as_of": "2026-08-24"}, {"as_of": "2026-08-25"}):
        rebuilt = admin_client.get("/api/v1/dashboard/overview", params=params).json()
        assert rebuilt["meta"]["analysis_status"] == "ready"
        assert rebuilt["data"]["fund_count"] == 1
        assert Decimal(rebuilt["data"]["company_index"]) == Decimal(1)

    assert admin_client.post(f"/api/v1/funds/{volatile_id}/enable").status_code == 200
    assert (
        admin_client.get("/api/v1/dashboard/overview").json()["meta"]["analysis_status"]
        == "pending"
    )
    _drain(admin_client, engine)
    enabled = admin_client.get("/api/v1/dashboard/overview").json()
    assert enabled["meta"]["analysis_status"] == "ready"
    assert Decimal(enabled["data"]["company_index"]) == Decimal("0.9")

    with Session(engine) as session:
        count = session.scalar(select(func.count(AnalysisRun.id)))
    assert admin_client.post(f"/api/v1/funds/{volatile_id}/enable").status_code == 200
    with Session(engine) as session:
        assert session.scalar(select(func.count(AnalysisRun.id))) == count


def test_disabling_all_funds_never_returns_old_company_index(
    admin_client, app_and_engine
):
    engine = app_and_engine[1]
    for fund_id in _seed_company(admin_client, engine):
        assert (
            admin_client.post(
                f"/api/v1/funds/{fund_id}/disable", json={"reason": "暂停"}
            ).status_code
            == 200
        )
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count(BackgroundJob.id)).where(
                    BackgroundJob.status == JobStatus.PENDING
                )
            )
            == 2
        )
    _drain(admin_client, engine)

    for params in ({}, {"as_of": "2026-08-25"}):
        result = admin_client.get("/api/v1/dashboard/overview", params=params).json()
        assert result["data"]["fund_count"] == 0
        assert result["data"]["company_index"] is None


def test_failed_scope_recalculation_hides_old_company_metrics(
    admin_client, app_and_engine, monkeypatch
):
    engine = app_and_engine[1]
    volatile_id, _ = _seed_company(admin_client, engine)
    admin_client.post(
        f"/api/v1/funds/{volatile_id}/disable", json={"reason": "调整范围"}
    )
    with Session(engine) as session:
        job = session.scalar(
            select(BackgroundJob).where(BackgroundJob.status == JobStatus.PENDING)
        )
        assert job is not None
        job.max_attempts = 1
        session.commit()

        def fail_analysis(*args, **kwargs):
            raise RuntimeError("scope recalculation failed")

        monkeypatch.setattr("app.imports.tasks.process_analysis_run", fail_analysis)
        process_next_job(session, admin_client.app.state.settings)

    result = admin_client.get("/api/v1/dashboard/overview").json()
    assert result["meta"]["analysis_status"] == "stale"
    assert result["data"]["company_index"] is None
