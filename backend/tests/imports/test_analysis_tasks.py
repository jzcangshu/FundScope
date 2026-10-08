import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.scope import RISK_RULE_RECALC_TRIGGER
from app.analytics.service import _lock_fund_for_analysis
from app.db.base import (
    AnalysisRunStatus,
    JobStatus,
    RiskEventStatus,
    RiskSeverity,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    BackgroundJob,
    CompanyMetricDaily,
    Fund,
    FundDailySnapshot,
    FundMetricDaily,
    RiskEvent,
    RiskRule,
    ValidationResult,
    ValuationVersion,
)
from app.imports.tasks import claim_next_job, process_next_job
from app.publishing import PublishingService


def test_postgresql_analysis_locks_fund_before_event_reconciliation() -> None:
    statements: list[object] = []
    fake_session = SimpleNamespace(
        bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        scalar=lambda statement: statements.append(statement),
    )

    _lock_fund_for_analysis(fake_session, 42)  # type: ignore[arg-type]

    assert len(statements) == 1
    assert getattr(statements[0], "_for_update_arg", None) is not None


def _published_history(session: Session) -> tuple[Fund, tuple[ValuationVersion, ...]]:
    fund = Fund(standard_name="分析任务测试产品")
    session.add(fund)
    session.flush()
    versions: list[ValuationVersion] = []
    for day, nav in (
        (date(2026, 8, 24), Decimal("1.00")),
        (date(2026, 8, 25), Decimal("0.90")),
    ):
        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=day,
            version_no=1,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(version)
        session.flush()
        session.add(
            FundDailySnapshot(
                valuation_version_id=version.id,
                net_asset_value=Decimal(100000),
                total_assets=Decimal(110000),
                unit_nav=nav,
                cumulative_unit_nav=nav,
            )
        )
        versions.append(version)
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="analysis_test",
                level="info",
                message="测试通过",
            )
        )
    session.add(
        RiskRule(
            rule_code="daily_loss",
            rule_type="daily_return",
            scope="all",
            threshold=Decimal("-0.05"),
            severity=RiskSeverity.WARNING,
            version="1",
            enabled=True,
        )
    )
    session.commit()
    service = PublishingService(session)
    for version in versions:
        service.publish_version(version.id, actor_user_id=None)
        session.commit()
    return fund, tuple(versions)


def test_analysis_job_persists_metrics_and_idempotent_risk_event(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        fund, _ = _published_history(session)
        result = process_next_job(session, app.state.settings)
        session.commit()

        assert result is not None
        assert result[0].status == JobStatus.SUCCEEDED
        runs = session.scalars(select(AnalysisRun).order_by(AnalysisRun.id)).all()
        for run in runs:
            jobs = session.scalars(
                select(BackgroundJob).where(
                    BackgroundJob.job_type == "process_analysis_run",
                    BackgroundJob.resource_id == str(run.id),
                )
            ).all()
            assert len(jobs) == 1
        run_id = session.scalar(
            select(AnalysisRun.id).order_by(AnalysisRun.id).limit(1)
        )
        assert run_id is not None
        assert session.get(AnalysisRun, run_id).status == AnalysisRunStatus.SUCCEEDED
        metrics = session.scalars(
            select(FundMetricDaily).where(
                FundMetricDaily.source_analysis_run_id == run_id
            )
        ).all()
        assert len(metrics) == 2
        assert (
            len(
                session.scalars(
                    select(CompanyMetricDaily).where(
                        CompanyMetricDaily.source_analysis_run_id == run_id
                    )
                ).all()
            )
            == 2
        )
        assert any(metric.daily_return == Decimal("-0.1") for metric in metrics)
        assert session.scalar(select(RiskEvent.id)) is not None

        second_result = process_next_job(session, app.state.settings)
        session.commit()

        assert second_result is not None
        assert second_result[0].status == JobStatus.SUCCEEDED
        assert second_result[1] is not None
        assert second_result[1].fund_metric_count == 1
        assert len(session.scalars(select(FundMetricDaily)).all()) == 3
        assert (
            len(
                session.scalars(
                    select(RiskEvent).where(RiskEvent.fund_id == fund.id)
                ).all()
            )
            == 1
        )


def test_analysis_uses_latest_applicable_risk_rule_version(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        _published_history(session)
        session.add(
            RiskRule(
                rule_code="daily_loss",
                rule_type="daily_return",
                scope="all",
                threshold=Decimal("-0.20"),
                severity=RiskSeverity.WARNING,
                version="2",
                enabled=True,
            )
        )
        session.commit()

        result = process_next_job(session, app.state.settings)
        session.commit()

        assert result is not None
        assert result[0].status == JobStatus.SUCCEEDED
        assert session.scalar(select(RiskEvent.id)) is None


def test_disabled_latest_risk_rule_does_not_reactivate_older_version(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        _published_history(session)
        session.add(
            RiskRule(
                rule_code="daily_loss",
                rule_type="daily_return",
                scope="all",
                threshold=Decimal("-0.05"),
                severity=RiskSeverity.WARNING,
                version="2",
                enabled=False,
            )
        )
        session.commit()

        result = process_next_job(session, app.state.settings)
        session.commit()

        assert result is not None
        assert result[0].status == JobStatus.SUCCEEDED
        assert session.scalar(select(RiskEvent.id)) is None


def test_changed_risk_evidence_creates_new_event_without_overwriting_history(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        fund, versions = _published_history(session)
        assert process_next_job(session, app.state.settings) is not None
        assert process_next_job(session, app.state.settings) is not None

        revision = ValuationVersion(
            fund_id=fund.id,
            valuation_date=versions[-1].valuation_date,
            version_no=2,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(revision)
        session.flush()
        session.add_all(
            [
                FundDailySnapshot(
                    valuation_version_id=revision.id,
                    net_asset_value=Decimal(100000),
                    total_assets=Decimal(110000),
                    unit_nav=Decimal("0.80"),
                    cumulative_unit_nav=Decimal("0.80"),
                ),
                ValidationResult(
                    valuation_version_id=revision.id,
                    rule_code="analysis_test",
                    level="info",
                    message="修订通过",
                ),
            ]
        )
        session.commit()
        PublishingService(session).publish_version(revision.id, actor_user_id=None)
        session.commit()

        assert process_next_job(session, app.state.settings) is not None
        events = session.scalars(
            select(RiskEvent).where(RiskEvent.fund_id == fund.id).order_by(RiskEvent.id)
        ).all()

        assert len(events) == 2
        assert events[0].evidence_snapshot != events[1].evidence_snapshot


def test_rule_recalculate_resolves_events_no_longer_triggered(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        fund, versions = _published_history(session)
        assert process_next_job(session, app.state.settings) is not None
        assert process_next_job(session, app.state.settings) is not None
        events = session.scalars(
            select(RiskEvent).where(RiskEvent.fund_id == fund.id)
        ).all()
        assert len(events) == 1
        assert events[0].status == RiskEventStatus.OPEN

        # A newer rule version with a looser threshold no longer triggers.
        session.add(
            RiskRule(
                rule_code="daily_loss",
                rule_type="daily_return",
                scope="all",
                threshold=Decimal("-0.20"),
                severity=RiskSeverity.WARNING,
                version="2",
                enabled=True,
            )
        )
        session.commit()
        run = PublishingService(session).queue_analysis_run(
            versions[-1].id,
            start_date=versions[0].valuation_date,
            actor_user_id=None,
            trigger_reason=RISK_RULE_RECALC_TRIGGER,
        )
        session.commit()

        assert process_next_job(session, app.state.settings) is not None
        session.commit()

        assert session.get(AnalysisRun, run.id).status == AnalysisRunStatus.SUCCEEDED
        event = session.get(RiskEvent, events[0].id)
        assert event.status == RiskEventStatus.RESOLVED
        assert event.handling_note == "风险规则重算后不再命中，系统自动关闭"
        assert event.handled_at is not None


def test_rule_recalculate_keeps_events_still_triggered(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        fund, versions = _published_history(session)
        assert process_next_job(session, app.state.settings) is not None
        assert process_next_job(session, app.state.settings) is not None
        events = session.scalars(
            select(RiskEvent).where(RiskEvent.fund_id == fund.id)
        ).all()
        assert len(events) == 1

        run = PublishingService(session).queue_analysis_run(
            versions[-1].id,
            start_date=versions[0].valuation_date,
            actor_user_id=None,
            trigger_reason=RISK_RULE_RECALC_TRIGGER,
        )
        session.commit()

        assert process_next_job(session, app.state.settings) is not None
        session.commit()

        assert session.get(AnalysisRun, run.id).status == AnalysisRunStatus.SUCCEEDED
        event = session.get(RiskEvent, events[0].id)
        assert event.status == RiskEventStatus.OPEN
        assert (
            len(
                session.scalars(
                    select(RiskEvent).where(RiskEvent.fund_id == fund.id)
                ).all()
            )
            == 1
        )


def test_revoke_analysis_succeeds_when_no_fund_record_remains_in_range(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        _, versions = _published_history(session)
        assert process_next_job(session, app.state.settings) is not None
        assert process_next_job(session, app.state.settings) is not None

        PublishingService(session).revoke_version(
            versions[-1].id,
            actor_user_id=None,
            reason="撤回错误估值",
        )
        session.commit()
        result = process_next_job(session, app.state.settings)
        session.commit()

        assert result is not None
        assert result[0].status == JobStatus.SUCCEEDED
        assert result[1] is not None
        assert result[1].fund_metric_count == 0
        assert session.get(ValuationVersion, versions[-1].id).status == (
            ValuationStatus.REVOKED
        )


def test_analysis_failure_marks_run_failed_and_keeps_published_version(
    app_and_engine: tuple[object, object],
    monkeypatch,
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        _, versions = _published_history(session)
        run_id = session.scalar(
            select(AnalysisRun.id).order_by(AnalysisRun.id).limit(1)
        )
        job = session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "process_analysis_run",
                BackgroundJob.resource_id == str(run_id),
            )
        )
        assert job is not None
        job.max_attempts = 1
        session.commit()

        def fail(*args: object, **kwargs: object) -> None:
            raise RuntimeError("analysis failed")

        monkeypatch.setattr("app.imports.tasks.process_analysis_run", fail)
        process_next_job(session, app.state.settings)
        session.commit()

        assert session.get(AnalysisRun, run_id).status == AnalysisRunStatus.FAILED
        assert (
            session.get(ValuationVersion, versions[-1].id).status
            == ValuationStatus.PUBLISHED
        )


def test_expired_analysis_job_at_attempt_limit_marks_run_failed(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        run = AnalysisRun(
            trigger_reason="test",
            methodology_version="v1",
            status=AnalysisRunStatus.RUNNING,
        )
        session.add(run)
        session.flush()
        session.add(
            BackgroundJob(
                job_type="process_analysis_run",
                resource_id=str(run.id),
                attempts=1,
                max_attempts=1,
                status=JobStatus.RUNNING,
                locked_at=now - timedelta(minutes=16),
                lease_token="expired-token",
            )
        )
        session.commit()

        assert claim_next_job(session, now=now) is None
        session.expire_all()

        assert session.get(AnalysisRun, run.id).status == AnalysisRunStatus.FAILED


def test_max_drawdown_risk_uses_only_history_available_on_each_date(
    app_and_engine,
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        fund = Fund(standard_name="回撤日期边界")
        session.add(fund)
        session.flush()
        publisher = PublishingService(session)
        for day, nav in enumerate(("1", "1.1", "0.7", "1.2"), start=1):
            version = ValuationVersion(
                fund_id=fund.id,
                valuation_date=date(2026, 1, day),
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
            publisher.publish_version(
                version.id, actor_user_id=None, schedule_analysis=False
            )
        session.add(
            RiskRule(
                rule_code="historical_drawdown",
                rule_type="max_drawdown",
                scope="all",
                threshold=Decimal("-0.2"),
                severity=RiskSeverity.WARNING,
                version="1",
                enabled=True,
            )
        )
        publisher.queue_analysis_run(
            version.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 4),
            actor_user_id=None,
            trigger_reason="test",
        )
        session.commit()

        result = process_next_job(session, app.state.settings)
        assert result[0].status == JobStatus.SUCCEEDED
        events = session.scalars(
            select(RiskEvent).order_by(RiskEvent.valuation_date)
        ).all()

        assert [event.valuation_date for event in events] == [
            date(2026, 1, 3),
            date(2026, 1, 4),
        ]
        observed = [
            json.loads(event.evidence_snapshot)["observed_value"] for event in events
        ]
        assert Decimal(observed[0]) < Decimal("-0.36")
        assert observed[0] == observed[1]
