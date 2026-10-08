"""Persisted analysis for the currently published valuation data."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analytics.company import CompanyMetric, calculate_company_index
from app.analytics.concentration import calculate_concentration
from app.analytics.drawdown import calculate_drawdown
from app.analytics.nav import calculate_nav_series
from app.analytics.scope import (
    COMPANY_SCOPE_TRIGGER,
    RISK_RULE_RECALC_TRIGGER,
    lock_company_scope,
)
from app.db.base import (
    AnalysisRunStatus,
    AuditResult,
    RiskEventStatus,
    RiskSeverity,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    AuditLog,
    CompanyMetricDaily,
    Fund,
    FundDailySnapshot,
    FundMetricDaily,
    PositionDaily,
    RiskEvent,
    RiskRule,
    ValuationVersion,
)
from app.risk.evaluator import evaluate_risk_rules


@dataclass(frozen=True, slots=True)
class AnalysisProcessResult:
    analysis_run_id: int
    fund_metric_count: int
    company_metric_count: int
    risk_event_count: int


@dataclass(frozen=True, slots=True)
class _PublishedRecord:
    version: ValuationVersion
    snapshot: FundDailySnapshot


def process_analysis_run(
    session: Session, analysis_run_id: int
) -> AnalysisProcessResult:
    """Rebuild the affected date range from published data in one transaction."""

    run = session.get(AnalysisRun, analysis_run_id)
    if run is None:
        raise ValueError("analysis_run_not_found")
    if run.status not in {AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING}:
        raise ValueError("analysis_run_not_queued")

    run.status = AnalysisRunStatus.RUNNING
    run.started_at = datetime.now(UTC)
    session.flush()

    lock_company_scope(session)
    records = _published_records(session)
    trigger = (
        session.get(ValuationVersion, run.trigger_version_id)
        if run.trigger_version_id is not None
        else None
    )
    if trigger is None and run.trigger_reason != COMPANY_SCOPE_TRIGGER:
        raise ValueError("analysis_trigger_version_not_found")
    start_date = run.input_start_date or (
        trigger.valuation_date if trigger else date.min
    )
    end_date = run.input_end_date
    grouped_records = _by_fund(records)
    trigger_records = grouped_records.get(trigger.fund_id, ()) if trigger else ()
    affected_records = tuple(
        item
        for item in trigger_records
        if _date_in_range(item.version.valuation_date, start_date, end_date)
    )
    if not affected_records and trigger and trigger.status == ValuationStatus.PUBLISHED:
        raise ValueError("analysis_input_not_found")

    session.execute(
        delete(FundMetricDaily).where(FundMetricDaily.source_analysis_run_id == run.id)
    )
    session.execute(
        delete(CompanyMetricDaily).where(
            CompanyMetricDaily.source_analysis_run_id == run.id
        )
    )
    positions = _group_positions(session, affected_records)
    rules = tuple(session.scalars(select(RiskRule).order_by(RiskRule.id)).all())
    if trigger is not None:
        _lock_fund_for_analysis(session, trigger.fund_id)
    existing_events = (
        _open_risk_events(
            session,
            fund_id=trigger.fund_id,
            start_date=start_date,
            end_date=end_date or affected_records[-1].version.valuation_date,
        )
        if affected_records and trigger is not None
        else {}
    )

    fund_metrics: list[FundMetricDaily] = []
    risk_events: list[RiskEvent] = []
    company_inputs: list[dict[str, object]] = []
    for fund_id, fund_records in grouped_records.items():
        nav_records = [
            {
                "valuation_date": item.version.valuation_date,
                "unit_nav": item.snapshot.unit_nav,
                "cumulative_unit_nav": item.snapshot.cumulative_unit_nav,
                "cumulative_payout": item.snapshot.cumulative_payout,
            }
            for item in fund_records
        ]
        nav = calculate_nav_series(nav_records)
        nav_by_date = {point.valuation_date: point for point in nav.points}

        for item in fund_records:
            day = item.version.valuation_date
            nav_point = nav_by_date[day]
            company_inputs.append(
                {
                    "fund_id": fund_id,
                    "valuation_date": day,
                    "net_asset_value": item.snapshot.net_asset_value,
                    "daily_return": nav_point.daily_return,
                }
            )

        if trigger is None or fund_id != trigger.fund_id:
            continue
        drawdown = calculate_drawdown(
            [
                {
                    "valuation_date": point.valuation_date,
                    "adjusted_nav": point.adjusted_nav,
                }
                for point in nav.points
            ]
        )
        drawdown_by_date = {point.valuation_date: point for point in drawdown.points}
        for item in affected_records:
            day = item.version.valuation_date
            nav_point = nav_by_date[day]
            drawdown_point = drawdown_by_date[day]
            concentration = calculate_concentration(
                positions.get(item.version.id, ()),
                net_asset_value=item.snapshot.net_asset_value,
            )
            asset_ratio = _ratio(
                item.snapshot.total_assets, item.snapshot.net_asset_value
            )
            fund_metrics.append(
                FundMetricDaily(
                    fund_id=fund_id,
                    valuation_date=day,
                    source_analysis_run_id=run.id,
                    daily_return=nav_point.daily_return,
                    cumulative_return=nav_point.cumulative_return,
                    drawdown=drawdown_point.drawdown,
                    historical_peak=drawdown_point.peak_value,
                    concentration=concentration.hhi,
                    asset_ratio=asset_ratio,
                )
            )
            risk_events.extend(
                _evaluate_events(
                    session,
                    rules,
                    run,
                    existing_events,
                    fund_id=fund_id,
                    day=day,
                    metrics={
                        "daily_return": nav_point.daily_return,
                        "drawdown": drawdown_point.drawdown,
                        "current_drawdown": drawdown_point.drawdown,
                        "max_drawdown": drawdown_point.max_drawdown,
                        "concentration": concentration.hhi,
                        "single_position_weight": concentration.max_single_weight,
                        "top_five_weight": concentration.top_five_weight,
                    },
                )
            )
    company_metrics = _company_metrics(
        company_inputs, fund_ids=tuple(_active_fund_ids(session))
    )
    resolved_event_count = 0
    if (
        run.trigger_reason == RISK_RULE_RECALC_TRIGGER
        and trigger is not None
        and affected_records
    ):
        resolved_event_count = _resolve_unmatched_events(
            session,
            fund_id=trigger.fund_id,
            start_date=start_date,
            end_date=end_date or affected_records[-1].version.valuation_date,
            retriggered={id(event) for event in risk_events},
        )
    affected_company_metrics = tuple(
        metric
        for metric in company_metrics
        if _date_in_range(metric.valuation_date, start_date, end_date)
    )
    session.add_all(fund_metrics)
    session.add_all(
        CompanyMetricDaily(
            valuation_date=metric.valuation_date,
            source_analysis_run_id=run.id,
            company_index=metric.company_index,
            company_daily_return=metric.company_daily_return,
            effective_fund_count=metric.effective_fund_count,
            total_net_assets=metric.total_net_assets,
        )
        for metric in affected_company_metrics
    )
    session.flush()
    run.input_end_date = max(
        (metric.valuation_date for metric in affected_company_metrics),
        default=start_date,
    )
    run.status = AnalysisRunStatus.SUCCEEDED
    run.ended_at = datetime.now(UTC)
    session.add(
        AuditLog(
            action="analysis.completed",
            resource_type="analysis_run",
            resource_id=str(run.id),
            summary={
                "fund_metric_count": len(fund_metrics),
                "company_metric_count": len(affected_company_metrics),
                "risk_event_count": len(risk_events),
                "risk_event_resolved_count": resolved_event_count,
            },
            result=AuditResult.SUCCESS,
        )
    )
    session.flush()
    return AnalysisProcessResult(
        analysis_run_id=run.id,
        fund_metric_count=len(fund_metrics),
        company_metric_count=len(affected_company_metrics),
        risk_event_count=len(risk_events),
    )


def _published_records(session: Session) -> tuple[_PublishedRecord, ...]:
    rows = session.execute(
        select(ValuationVersion, FundDailySnapshot)
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == "active",
        )
        .order_by(ValuationVersion.fund_id, ValuationVersion.valuation_date)
    )
    return tuple(_PublishedRecord(version, snapshot) for version, snapshot in rows)


def _by_fund(
    records: tuple[_PublishedRecord, ...],
) -> dict[int, tuple[_PublishedRecord, ...]]:
    grouped: dict[int, list[_PublishedRecord]] = defaultdict(list)
    for item in records:
        grouped[item.version.fund_id].append(item)
    return {fund_id: tuple(items) for fund_id, items in grouped.items()}


def _group_positions(
    session: Session, records: tuple[_PublishedRecord, ...]
) -> dict[int, tuple[PositionDaily, ...]]:
    version_ids = [item.version.id for item in records]
    if not version_ids:
        return {}
    grouped: dict[int, list[PositionDaily]] = defaultdict(list)
    for row in session.scalars(
        select(PositionDaily)
        .where(PositionDaily.valuation_version_id.in_(version_ids))
        .order_by(PositionDaily.valuation_version_id, PositionDaily.id)
    ):
        grouped[row.valuation_version_id].append(row)
    return {version_id: tuple(rows) for version_id, rows in grouped.items()}


def _active_fund_ids(session: Session) -> tuple[int, ...]:
    return tuple(
        session.scalars(
            select(Fund.id).where(Fund.status == "active").order_by(Fund.id)
        ).all()
    )


def _company_metrics(
    records: list[dict[str, object]], *, fund_ids: tuple[int, ...]
) -> tuple[CompanyMetric, ...]:
    if not records:
        return ()
    return calculate_company_index(records, fund_ids=list(fund_ids))


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator in (None, Decimal(0)):
        return None
    return numerator / denominator


def _date_in_range(day: date, start_date: date, end_date: date | None) -> bool:
    return day >= start_date and (end_date is None or day <= end_date)


def _lock_fund_for_analysis(session: Session, fund_id: int) -> None:
    """Serialize event reconciliation for one fund in production."""

    statement = select(Fund.id).where(Fund.id == fund_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        statement = statement.with_for_update()
    session.scalar(statement)


def _open_risk_events(
    session: Session,
    *,
    fund_id: int,
    start_date: date,
    end_date: date,
) -> dict[tuple[int, int, date, str], RiskEvent]:
    events = session.scalars(
        select(RiskEvent).where(
            RiskEvent.fund_id == fund_id,
            RiskEvent.valuation_date >= start_date,
            RiskEvent.valuation_date <= end_date,
            RiskEvent.status.in_((RiskEventStatus.OPEN, RiskEventStatus.ACKNOWLEDGED)),
        )
    )
    existing: dict[tuple[int, int, date, str], RiskEvent] = {}
    for event in events:
        evidence_key = _evidence_key_from_snapshot(event.evidence_snapshot)
        if evidence_key is not None:
            existing.setdefault(
                (event.risk_rule_id, fund_id, event.valuation_date, evidence_key),
                event,
            )
    return existing


def _resolve_unmatched_events(
    session: Session,
    *,
    fund_id: int,
    start_date: date,
    end_date: date,
    retriggered: set[int],
) -> int:
    """Close window events the recalculated rules no longer trigger.

    Only used by manual rule recalculation runs: events stay in the audit
    trail as resolved instead of being deleted, and anything the current
    rule versions still trigger (present in ``retriggered`` by object id)
    keeps its open/acknowledged state.
    """

    events = session.scalars(
        select(RiskEvent).where(
            RiskEvent.fund_id == fund_id,
            RiskEvent.valuation_date >= start_date,
            RiskEvent.valuation_date <= end_date,
            RiskEvent.status.in_((RiskEventStatus.OPEN, RiskEventStatus.ACKNOWLEDGED)),
        )
    ).all()
    now = datetime.now(UTC)
    resolved = 0
    for event in events:
        if id(event) in retriggered:
            continue
        event.status = RiskEventStatus.RESOLVED
        event.handling_note = "风险规则重算后不再命中，系统自动关闭"
        event.handled_at = now
        resolved += 1
    return resolved


def _effective_rules(rules: tuple[RiskRule, ...], day: date) -> tuple[RiskRule, ...]:
    latest: dict[str, RiskRule] = {}
    for rule in rules:
        if (
            (rule.valid_from is None or rule.valid_from <= day)
            and (rule.valid_to is None or day <= rule.valid_to)
            and rule.scope in {"all", "fund"}
        ):
            latest[rule.rule_code] = rule
    return tuple(rule for rule in latest.values() if rule.enabled)


def _evidence_key(*, observed_value: object, threshold: object, message: object) -> str:
    return json.dumps(
        {
            "message": str(message),
            "observed_value": str(observed_value),
            "threshold": str(threshold),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _evidence_key_from_snapshot(snapshot: str | None) -> str | None:
    if snapshot is None:
        return None
    try:
        payload = json.loads(snapshot)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not {
        "message",
        "observed_value",
        "threshold",
    }.issubset(payload):
        return None
    return _evidence_key(
        observed_value=payload["observed_value"],
        threshold=payload["threshold"],
        message=payload["message"],
    )


def _evaluate_events(
    session: Session,
    rules: tuple[RiskRule, ...],
    run: AnalysisRun,
    existing_events: dict[tuple[int, int, date, str], RiskEvent],
    *,
    fund_id: int,
    day: date,
    metrics: dict[str, object],
) -> tuple[RiskEvent, ...]:
    applicable = _effective_rules(rules, day)
    if not applicable:
        return ()
    events = evaluate_risk_rules(
        metrics,
        applicable,
        valuation_date_value=day,
        fund_id=fund_id,
    )
    persisted: list[RiskEvent] = []
    rules_by_code = {rule.rule_code: rule for rule in applicable}
    for event in events:
        rule = rules_by_code[event.rule_code]
        stable_evidence = _evidence_key(
            observed_value=event.observed_value,
            threshold=event.threshold,
            message=event.message,
        )
        key = (rule.id, fund_id, day, stable_evidence)
        existing = existing_events.get(key)
        evidence = json.dumps(
            {
                "analysis_run_id": run.id,
                "observed_value": str(event.observed_value),
                "threshold": str(event.threshold),
                "message": event.message,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        now = datetime.now(UTC)
        if existing is None:
            existing = RiskEvent(
                risk_rule_id=rule.id,
                fund_id=fund_id,
                valuation_date=day,
                severity=RiskSeverity(event.severity),
                status=RiskEventStatus.OPEN,
                first_triggered_at=now,
                last_triggered_at=now,
                evidence_snapshot=evidence,
            )
            session.add(existing)
            existing_events[key] = existing
        else:
            existing.last_triggered_at = now
            existing.evidence_snapshot = evidence
        persisted.append(existing)
    return tuple(persisted)


__all__ = ["AnalysisProcessResult", "process_analysis_run"]
