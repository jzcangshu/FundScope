"""Read-only dashboard queries over published valuation versions."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, func, over, select
from sqlalchemy.orm import Session, aliased

from app.analytics.drawdown import (
    RANGE_METHODOLOGY,
    RangeDrawdownResult,
    calculate_drawdown,
    calculate_range_drawdown,
    calendar_start_date,
)
from app.analytics.nav import calculate_nav_series
from app.analytics.scope import COMPANY_SCOPE_TRIGGER
from app.analytics.user_scope import scoped_company_history
from app.auth.dependencies import AuthContext, get_auth_context, get_db
from app.auth.fund_access import require_fund_access, visible_fund_ids
from app.benchmark.query import (
    align_benchmark_to_dates,
    benchmark_return,
    benchmark_series_for_range,
    benchmark_state,
)
from app.db.base import (
    AnalysisRunStatus,
    FundStatus,
    RiskEventStatus,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    CompanyMetricDaily,
    Fund,
    FundAlias,
    FundDailySnapshot,
    FundMetricDaily,
    PositionDaily,
    RiskEvent,
    ShareClass,
    ValidationResult,
    ValuationVersion,
)

router = APIRouter(tags=["dashboard"])
DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[AuthContext, Depends(get_auth_context)]


@dataclass(frozen=True, slots=True)
class _VersionView:
    fund: Fund
    snapshot: FundDailySnapshot | None
    quality_status: str
    analysis_status: str
    analysis_run: AnalysisRun | None
    metric: FundMetricDaily | None


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _published_versions(
    session: Session,
    *,
    fund_id: int | None = None,
    fund_ids: tuple[int, ...] | frozenset[int] | None = None,
    as_of: date | None = None,
) -> list[ValuationVersion]:
    """Return the most recent PUBLISHED version per active fund.

    The previous implementation loaded every published version into Python and
    deduplicated by fund_id there. On a real dataset that scan produced
    hundreds of thousands of rows per call, which OOMed the worker whenever
    list_funds walked the fund universe. The ROW_NUMBER() window function
    pushes the same "latest per fund" logic into SQL.
    """

    rank = over(
        func.row_number(),
        partition_by=ValuationVersion.fund_id,
        order_by=(
            ValuationVersion.valuation_date.desc(),
            ValuationVersion.id.desc(),
        ),
    )
    subquery = (
        select(ValuationVersion.id, rank.label("_rank"))
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(ValuationVersion.status == ValuationStatus.PUBLISHED)
        .where(Fund.status == FundStatus.ACTIVE)
    )
    if fund_id is not None:
        subquery = subquery.where(ValuationVersion.fund_id == fund_id)
    if fund_ids is not None:
        subquery = subquery.where(ValuationVersion.fund_id.in_(fund_ids))
    if as_of is not None:
        subquery = subquery.where(ValuationVersion.valuation_date == as_of)
    subquery = subquery.subquery()
    statement = (
        select(ValuationVersion)
        .join(subquery, subquery.c.id == ValuationVersion.id)
        .where(subquery.c._rank == 1)
        .order_by(ValuationVersion.fund_id)
    )
    return list(session.scalars(statement))


def _version_for_fund(
    session: Session, fund_id: int, as_of: date | None
) -> ValuationVersion | None:
    versions = _published_versions(session, fund_id=fund_id, as_of=as_of)
    return versions[0] if versions else None


def _snapshot(session: Session, version_id: int) -> FundDailySnapshot | None:
    return session.scalar(
        select(FundDailySnapshot).where(
            FundDailySnapshot.valuation_version_id == version_id
        )
    )


def _quality_status(session: Session, version_id: int) -> str:
    levels = session.scalars(
        select(ValidationResult.level).where(
            ValidationResult.valuation_version_id == version_id,
            ValidationResult.ignored.is_(False),
        )
    ).all()
    if any(str(level) == "critical" for level in levels):
        return "warning"
    if any(str(level) == "warning" for level in levels):
        return "warning"
    return "valid"


def _analysis_run_for_version(
    session: Session, version: ValuationVersion
) -> AnalysisRun | None:
    return session.scalar(
        select(AnalysisRun)
        .join(FundMetricDaily, FundMetricDaily.source_analysis_run_id == AnalysisRun.id)
        .where(
            AnalysisRun.status == AnalysisRunStatus.SUCCEEDED,
            FundMetricDaily.fund_id == version.fund_id,
            FundMetricDaily.valuation_date == version.valuation_date,
        )
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )


def _analysis_status(session: Session, version: ValuationVersion) -> str:
    latest = session.scalar(
        select(AnalysisRun)
        .join(
            ValuationVersion,
            ValuationVersion.id == AnalysisRun.trigger_version_id,
        )
        .where(
            ValuationVersion.fund_id == version.fund_id,
            AnalysisRun.input_start_date.is_not(None),
            AnalysisRun.input_start_date <= version.valuation_date,
            (AnalysisRun.input_end_date.is_(None))
            | (AnalysisRun.input_end_date >= version.valuation_date),
        )
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )
    if latest is None:
        return "pending"
    if latest.status == AnalysisRunStatus.SUCCEEDED:
        return "ready" if _analysis_run_for_version(session, version) else "stale"
    if latest.status == AnalysisRunStatus.FAILED:
        return "stale"
    return "pending"


def _metric_for_version(
    session: Session, version: ValuationVersion
) -> FundMetricDaily | None:
    run = _analysis_run_for_version(session, version)
    if run is None or run.status != AnalysisRunStatus.SUCCEEDED:
        return None
    return session.scalar(
        select(FundMetricDaily).where(
            FundMetricDaily.fund_id == version.fund_id,
            FundMetricDaily.valuation_date == version.valuation_date,
            FundMetricDaily.source_analysis_run_id == run.id,
        )
    )


def _company_metric_conditions(
    *,
    minimum_run_id: int | None = None,
    as_of: date | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[ColumnElement[bool]]:
    """Filters shared by every company-metric query: rows from succeeded
    analysis runs, restricted to dates where at least one active fund has a
    published valuation on the same day."""
    published_date_exists = (
        select(ValuationVersion.id)
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == FundStatus.ACTIVE,
            ValuationVersion.valuation_date == CompanyMetricDaily.valuation_date,
        )
        .exists()
    )
    conditions: list[ColumnElement[bool]] = [
        AnalysisRun.status == AnalysisRunStatus.SUCCEEDED,
        published_date_exists,
    ]
    if as_of is not None:
        conditions.append(CompanyMetricDaily.valuation_date == as_of)
    if start_date is not None:
        conditions.append(CompanyMetricDaily.valuation_date >= start_date)
    if end_date is not None:
        conditions.append(CompanyMetricDaily.valuation_date <= end_date)
    if minimum_run_id is not None:
        conditions.append(AnalysisRun.id >= minimum_run_id)
    return conditions


def _latest_company_metric(
    session: Session, as_of: date | None, *, minimum_run_id: int | None = None
) -> tuple[CompanyMetricDaily | None, AnalysisRun | None]:
    statement = (
        select(CompanyMetricDaily, AnalysisRun)
        .join(AnalysisRun, AnalysisRun.id == CompanyMetricDaily.source_analysis_run_id)
        .where(*_company_metric_conditions(as_of=as_of, minimum_run_id=minimum_run_id))
        .order_by(
            CompanyMetricDaily.valuation_date.desc(),
            AnalysisRun.id.desc(),
            CompanyMetricDaily.id.desc(),
        )
    )
    row = session.execute(statement.limit(1)).first()
    if row is None:
        return None, None
    metric, run = row
    return metric, run


def _version_views(
    session: Session, versions: list[ValuationVersion]
) -> dict[int, _VersionView]:
    if not versions:
        return {}
    version_ids = tuple(version.id for version in versions)
    fund_ids = tuple({version.fund_id for version in versions})
    start_date = min(version.valuation_date for version in versions)
    end_date = max(version.valuation_date for version in versions)

    funds = {
        fund.id: fund
        for fund in session.scalars(select(Fund).where(Fund.id.in_(fund_ids)))
    }
    snapshots = {
        snapshot.valuation_version_id: snapshot
        for snapshot in session.scalars(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id.in_(version_ids)
            )
        )
    }
    warning_versions = set(
        session.scalars(
            select(ValidationResult.valuation_version_id)
            .where(
                ValidationResult.valuation_version_id.in_(version_ids),
                ValidationResult.level.in_(("critical", "warning")),
                ValidationResult.ignored.is_(False),
            )
            .distinct()
        )
    )

    trigger_version = aliased(ValuationVersion)
    covering_runs: dict[int, list[AnalysisRun]] = {}
    rows = session.execute(
        select(AnalysisRun, trigger_version.fund_id)
        .join(trigger_version, trigger_version.id == AnalysisRun.trigger_version_id)
        .where(
            trigger_version.fund_id.in_(fund_ids),
            AnalysisRun.input_start_date.is_not(None),
            AnalysisRun.input_start_date <= end_date,
            (AnalysisRun.input_end_date.is_(None))
            | (AnalysisRun.input_end_date >= start_date),
        )
        .order_by(AnalysisRun.id.desc())
    )
    for run, run_fund_id in rows:
        covering_runs.setdefault(run_fund_id, []).append(run)

    metrics: dict[tuple[int, date], FundMetricDaily] = {}
    metric_runs: dict[tuple[int, date], AnalysisRun] = {}
    # Deduplicate inside the database: keep only each (fund, date)'s metric row
    # from the latest succeeded run so long windows never materialize the full
    # run history in Python (same fix as dashboard/series).
    rank = (
        func.row_number()
        .over(
            partition_by=(FundMetricDaily.fund_id, FundMetricDaily.valuation_date),
            order_by=(
                FundMetricDaily.source_analysis_run_id.desc(),
                FundMetricDaily.id.desc(),
            ),
        )
        .label("rn")
    )
    ranked = (
        select(FundMetricDaily.id.label("metric_id"), rank)
        .join(AnalysisRun, AnalysisRun.id == FundMetricDaily.source_analysis_run_id)
        .where(
            FundMetricDaily.fund_id.in_(fund_ids),
            FundMetricDaily.valuation_date >= start_date,
            FundMetricDaily.valuation_date <= end_date,
            AnalysisRun.status == AnalysisRunStatus.SUCCEEDED,
        )
        .subquery()
    )
    metric_rows = session.execute(
        select(FundMetricDaily, AnalysisRun)
        .join(ranked, ranked.c.metric_id == FundMetricDaily.id)
        .join(AnalysisRun, AnalysisRun.id == FundMetricDaily.source_analysis_run_id)
        .where(ranked.c.rn == 1)
    )
    version_keys = {(version.fund_id, version.valuation_date) for version in versions}
    for metric, run in metric_rows:
        key = (metric.fund_id, metric.valuation_date)
        if key in version_keys and key not in metrics:
            metrics[key] = metric
            metric_runs[key] = run

    views: dict[int, _VersionView] = {}
    for version in versions:
        key = (version.fund_id, version.valuation_date)
        latest = next(
            (
                run
                for run in covering_runs.get(version.fund_id, ())
                if run.input_start_date is not None
                and run.input_start_date <= version.valuation_date
                and (
                    run.input_end_date is None
                    or run.input_end_date >= version.valuation_date
                )
            ),
            None,
        )
        if latest is None:
            analysis_status = "pending"
        elif latest.status == AnalysisRunStatus.SUCCEEDED:
            analysis_status = "ready" if key in metrics else "stale"
        elif latest.status == AnalysisRunStatus.FAILED:
            analysis_status = "stale"
        else:
            analysis_status = "pending"
        views[version.id] = _VersionView(
            fund=funds[version.fund_id],
            snapshot=snapshots.get(version.id),
            quality_status="warning" if version.id in warning_versions else "valid",
            analysis_status=analysis_status,
            analysis_run=metric_runs.get(key) if analysis_status == "ready" else None,
            metric=metrics.get(key) if analysis_status == "ready" else None,
        )
    return views


# ---------------------------------------------------------------------------
# Benchmark helpers
# ---------------------------------------------------------------------------

BENCHMARK_TS_CODE = "000300.SH"
BENCHMARK_NAME = "沪深300"


def _benchmark_data(
    session: Session, start: date, end: date
) -> dict[str, object] | None:
    """Return benchmark series data for the given date range, or None."""

    rows = benchmark_series_for_range(session, start, end, BENCHMARK_TS_CODE)
    if not rows:
        return None
    return {
        "ts_code": BENCHMARK_TS_CODE,
        "name": BENCHMARK_NAME,
        "points": [
            {
                "trade_date": row.trade_date.isoformat(),
                "close": _decimal(row.close),
            }
            for row in rows
        ],
    }


@router.get("/api/v1/benchmark")
def benchmark_info(
    context: CurrentContext,
    session: DatabaseSession,
) -> dict[str, object]:
    """Return benchmark index metadata and sync status."""

    state = benchmark_state(session)
    if state is None or state.total_rows == 0:
        return {
            "data": {
                "ts_code": BENCHMARK_TS_CODE,
                "name": BENCHMARK_NAME,
                "source": "tushare",
                "methodology": "price_index",
                "coverage": None,
                "sync": None,
            },
        }
    return {
        "data": {
            "ts_code": state.ts_code,
            "name": BENCHMARK_NAME,
            "source": "tushare",
            "methodology": "price_index",
            "coverage": {
                "earliest": state.earliest_trade_date.isoformat()
                if state.earliest_trade_date
                else None,
                "latest": state.last_trade_date.isoformat()
                if state.last_trade_date
                else None,
                "total_rows": state.total_rows,
            },
            "sync": {
                "last_success_at": state.last_success_at.isoformat()
                if state.last_success_at
                else None,
                "last_attempt_at": state.last_attempt_at.isoformat()
                if state.last_attempt_at
                else None,
                "error": state.last_error,
            },
        },
    }


@router.get("/api/v1/dashboard/overview")
def overview(
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    allowed_ids = visible_fund_ids(session, context.user)
    fund_filters = [Fund.status == FundStatus.ACTIVE]
    if allowed_ids is not None:
        fund_filters.append(Fund.id.in_(allowed_ids))
    total = session.scalar(select(func.count(Fund.id)).where(*fund_filters)) or 0
    versions = _published_versions(session, as_of=as_of, fund_ids=allowed_ids)
    views = _version_views(session, versions)
    selected_fund_ids = {version.fund_id for version in versions}
    snapshots = [
        view.snapshot
        for version in versions
        if (view := views[version.id]).snapshot is not None
    ]
    total_net_assets = sum(
        (
            snapshot.net_asset_value
            for snapshot in snapshots
            if snapshot.net_asset_value is not None
        ),
        Decimal(0),
    )
    risk_filters = [
        RiskEvent.status.in_((RiskEventStatus.OPEN, RiskEventStatus.ACKNOWLEDGED))
    ]
    if allowed_ids is not None:
        risk_filters.append(RiskEvent.fund_id.in_(allowed_ids))
    if as_of is not None:
        risk_filters.append(RiskEvent.valuation_date == as_of)
    open_risk_count = (
        session.scalar(select(func.count(RiskEvent.id)).where(*risk_filters)) or 0
    )
    analysis_states = [views[version.id].analysis_status for version in versions]
    scope_run = session.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.trigger_reason == COMPANY_SCOPE_TRIGGER)
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )
    if allowed_ids is not None:
        scope_run = None
    if scope_run is not None:
        analysis_states.append(
            "ready"
            if scope_run.status == AnalysisRunStatus.SUCCEEDED
            else "stale"
            if scope_run.status == AnalysisRunStatus.FAILED
            else "pending"
        )
    analysis_status = (
        "stale"
        if "stale" in analysis_states
        else "pending"
        if "pending" in analysis_states or not analysis_states
        else "ready"
    )
    company_metric, company_run = (
        _latest_company_metric(
            session, as_of, minimum_run_id=scope_run.id if scope_run else None
        )
        if analysis_status == "ready" and allowed_ids is None
        else (None, None)
    )
    if allowed_ids is not None:
        metrics, _ = scoped_company_history(session, allowed_ids, end=as_of)
        company_metric = next(
            (
                metric
                for metric in reversed(metrics)
                if as_of is None or metric.valuation_date == as_of
            ),
            None,
        )
    return {
        "data": {
            "as_of": as_of.isoformat() if as_of else None,
            "total_net_assets": _decimal(total_net_assets) if snapshots else None,
            "fund_count": len(selected_fund_ids),
            "company_index": _decimal(company_metric.company_index)
            if company_metric
            else None,
            "company_daily_return": _decimal(company_metric.company_daily_return)
            if company_metric
            else None,
            "risk_event_count": open_risk_count,
            "quality_status": (
                "warning"
                if any(
                    views[version.id].quality_status == "warning"
                    for version in versions
                )
                else "valid"
            ),
            "funds": _overview_funds(versions, views),
        },
        "meta": {
            "as_of": as_of.isoformat() if as_of else None,
            "coverage": {"available": len(selected_fund_ids), "total": total},
            "analysis_status": analysis_status,
            "analysis_run_id": company_run.id if company_run else None,
        },
    }


def _scoped_dashboard_series(
    session: Session,
    fund_ids: frozenset[int],
    *,
    start: date | None,
    end: date | None,
) -> dict[str, object]:
    latest = session.scalar(
        select(func.max(ValuationVersion.valuation_date))
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == FundStatus.ACTIVE,
            Fund.id.in_(fund_ids),
        )
    )
    end_date = end or latest or datetime.now(UTC).date()
    start_date = start or (end_date - timedelta(days=365))
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if (end_date - start_date).days > 365 * 5:
        raise HTTPException(
            status_code=422, detail="窗口跨度不能超过 5 年，请缩小范围或使用导出功能"
        )
    history, asset_totals = scoped_company_history(session, fund_ids, end=end_date)
    metrics = [
        metric for metric in history if start_date <= metric.valuation_date <= end_date
    ]
    drawdown = calculate_drawdown(
        [
            {
                "valuation_date": metric.valuation_date,
                "adjusted_nav": metric.company_index,
            }
            for metric in metrics
        ]
    )
    drawdown_by_date = {
        point.valuation_date: point.drawdown for point in drawdown.points
    }
    points = [
        {
            "valuation_date": metric.valuation_date.isoformat(),
            "company_index": _decimal(metric.company_index),
            "company_daily_return": _decimal(metric.company_daily_return),
            "drawdown": _decimal(drawdown_by_date.get(metric.valuation_date)),
            "total_net_assets": _decimal(metric.total_net_assets),
            "total_assets": _decimal(asset_totals.get(metric.valuation_date)),
            "effective_fund_count": metric.effective_fund_count,
        }
        for metric in metrics
    ]
    return {
        "data": {
            "methodology": "daily_nav_weighted_index",
            "points": points,
            "benchmark": _benchmark_data(session, start_date, end_date),
        },
        "meta": {
            "coverage": {"available": len(points), "total": len(points)},
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "max_drawdown": _decimal(drawdown.max_drawdown),
            "current_drawdown": _decimal(drawdown.current_drawdown),
            "analysis_status": "ready" if points else "pending",
        },
    }


@router.get("/api/v1/dashboard/series")
def dashboard_series(
    context: CurrentContext,
    session: DatabaseSession,
    start: date | None = Query(default=None),  # noqa: B008
    end: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    """Return daily total assets alongside the company NAV index and drawdown."""

    allowed_ids = visible_fund_ids(session, context.user)
    if allowed_ids is not None:
        return _scoped_dashboard_series(session, allowed_ids, start=start, end=end)

    if start is not None and end is not None and (end - start).days > 365 * 5:
        raise HTTPException(
            status_code=422,
            detail="窗口跨度不能超过 5 年，请缩小范围或使用导出功能",
        )
    latest_date = session.scalar(
        select(func.max(ValuationVersion.valuation_date))
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == FundStatus.ACTIVE,
        )
    )
    scope_run = session.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.trigger_reason == COMPANY_SCOPE_TRIGGER)
        .order_by(AnalysisRun.id.desc())
        .limit(1)
    )
    if scope_run is not None and scope_run.status != AnalysisRunStatus.SUCCEEDED:
        return {
            "data": {"methodology": "daily_nav_weighted_index", "points": []},
            "meta": {
                "coverage": {"available": 0, "total": 0},
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "max_drawdown": None,
                "current_drawdown": None,
                "analysis_status": "stale"
                if scope_run.status == AnalysisRunStatus.FAILED
                else "pending",
            },
        }
    if latest_date is None:
        return {
            "data": {"methodology": "daily_nav_weighted_index", "points": []},
            "meta": {
                "coverage": {"available": 0, "total": 0},
                "start": None,
                "end": None,
                "max_drawdown": None,
                "current_drawdown": None,
                "analysis_status": "pending",
            },
        }
    end_date = end or latest_date
    start_date = start or (end_date - timedelta(days=365))
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if (end_date - start_date).days > 365 * 5:
        raise HTTPException(
            status_code=422,
            detail="窗口跨度不能超过 5 年，请缩小范围或使用导出功能",
        )

    # Deduplicate inside the database: keep only each date's latest metric row
    # so the endpoint never materializes the full run history in Python.
    rank = (
        func.row_number()
        .over(
            partition_by=CompanyMetricDaily.valuation_date,
            order_by=(
                CompanyMetricDaily.source_analysis_run_id.desc(),
                CompanyMetricDaily.id.desc(),
            ),
        )
        .label("rn")
    )
    ranked = (
        select(
            CompanyMetricDaily.valuation_date.label("valuation_date"),
            CompanyMetricDaily.company_index.label("company_index"),
            CompanyMetricDaily.company_daily_return.label("company_daily_return"),
            CompanyMetricDaily.total_net_assets.label("total_net_assets"),
            CompanyMetricDaily.effective_fund_count.label("effective_fund_count"),
            rank,
        )
        .join(AnalysisRun, AnalysisRun.id == CompanyMetricDaily.source_analysis_run_id)
        .where(
            *_company_metric_conditions(
                start_date=start_date,
                end_date=end_date,
                minimum_run_id=scope_run.id if scope_run is not None else None,
            )
        )
        .subquery()
    )
    metrics = session.execute(
        select(
            ranked.c.valuation_date,
            ranked.c.company_index,
            ranked.c.company_daily_return,
            ranked.c.total_net_assets,
            ranked.c.effective_fund_count,
        )
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.valuation_date)
    ).all()
    # As-of aggregation: on every metric date each active product contributes
    # its own latest published net_asset_value at or before that date. The
    # company caliber is uniform net assets by product decision — providers of
    # some funds report gross assets only intermittently, and mixing calibers
    # across funds would make the curve jump when a fund switches source.
    # Funds publish on uneven schedules, so a same-day-only sum would swing
    # with the number of funds that happened to publish, which reads as a
    # crash in the assets that never happened. Every value is a published
    # number; nothing is interpolated, and a product with no data yet is
    # simply not in the total yet.
    published_values = session.execute(
        select(
            ValuationVersion.fund_id,
            ValuationVersion.valuation_date,
            FundDailySnapshot.net_asset_value.label("value"),
        )
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .outerjoin(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == FundStatus.ACTIVE,
            ValuationVersion.valuation_date <= end_date,
        )
        .order_by(ValuationVersion.fund_id, ValuationVersion.valuation_date)
    ).all()
    fund_history: dict[int, tuple[list[date], list[Decimal]]] = {}
    for row in published_values:
        if row.value is None:
            continue
        dates, values = fund_history.setdefault(row.fund_id, ([], []))
        if dates and dates[-1] == row.valuation_date:
            continue
        dates.append(row.valuation_date)
        values.append(row.value)

    def total_assets_as_of(day: date) -> Decimal | None:
        total: Decimal | None = None
        for fund_dates, fund_values in fund_history.values():
            index = bisect_right(fund_dates, day) - 1
            if index >= 0:
                total = (
                    fund_values[index] if total is None else total + fund_values[index]
                )
        return total

    drawdown = calculate_drawdown(
        [
            {
                "valuation_date": metric.valuation_date,
                "adjusted_nav": metric.company_index,
            }
            for metric in metrics
        ]
    )
    drawdown_by_date = {point.valuation_date: point for point in drawdown.points}
    points = []
    for metric in metrics:
        point = drawdown_by_date[metric.valuation_date]
        points.append(
            {
                "valuation_date": metric.valuation_date.isoformat(),
                "company_index": _decimal(metric.company_index),
                "company_daily_return": _decimal(metric.company_daily_return),
                "drawdown": _decimal(point.drawdown),
                "total_net_assets": _decimal(metric.total_net_assets),
                "total_assets": _decimal(total_assets_as_of(metric.valuation_date)),
                "effective_fund_count": metric.effective_fund_count,
            }
        )
    return {
        "data": {
            "methodology": "daily_nav_weighted_index",
            "points": points,
            "benchmark": _benchmark_data(session, start_date, end_date),
        },
        "meta": {
            "coverage": {"available": len(points), "total": len(points)},
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "max_drawdown": _decimal(drawdown.max_drawdown),
            "current_drawdown": _decimal(drawdown.current_drawdown),
            "analysis_status": "ready",
        },
    }


# ---------------------------------------------------------------------------
# Range drawdown
# ---------------------------------------------------------------------------
# Every product row, the company chart and the export share this code path:
# the window is resolved first, then the standard drawdown is recomputed from
# that window's adjusted NAV with the running peak reset at its baseline.

#: Extra history loaded before a window start so the baseline valuation can
#: still be resolved for products that publish sparsely.
BASELINE_LOOKBACK_DAYS = 400
#: "全部" is the loaded trailing window, never a claim of inception coverage.
FULL_WINDOW_DAYS = 1826

RANGE_DRAWDOWN_KEYS = ("1m", "3m", "ytd", "1y", "all")


def _range_drawdown_payload(result: RangeDrawdownResult) -> dict[str, object]:
    """Serialise one window result: actual dates, caliber, status, reason."""

    return {
        "methodology": result.methodology,
        "status": result.status,
        "reason": result.reason,
        "target_start": result.target_start.isoformat()
        if result.target_start
        else None,
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "current_drawdown": _decimal(result.current_drawdown),
        "max_drawdown": _decimal(result.max_drawdown),
        "peak_date": result.peak_date.isoformat() if result.peak_date else None,
        "trough_date": result.trough_date.isoformat() if result.trough_date else None,
        "peak_value": _decimal(result.peak_value),
        "trough_value": _decimal(result.trough_value),
        "observed_points": result.observed_points,
        "missing_points": result.missing_points,
        "note": None,
    }


def _unavailable_drawdown(
    reason: str,
    *,
    target_start: date | None = None,
    end: date | None = None,
) -> dict[str, object]:
    """Explicitly unavailable window: never a zero standing in for missing history."""

    return {
        "methodology": RANGE_METHODOLOGY,
        "status": "insufficient",
        "reason": reason,
        "target_start": target_start.isoformat() if target_start else None,
        "start": None,
        "end": end.isoformat() if end else None,
        "current_drawdown": None,
        "max_drawdown": None,
        "peak_date": None,
        "trough_date": None,
        "peak_value": None,
        "trough_value": None,
        "observed_points": 0,
        "missing_points": 0,
        "note": None,
    }


def _adjusted_nav_points(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Adjusted (total-return) NAV per valuation day, same caliber as returns."""

    series = calculate_nav_series(rows)
    return [
        {"valuation_date": point.valuation_date, "adjusted_nav": point.adjusted_nav}
        for point in series.points
    ]


def _published_nav_rows(
    session: Session,
    fund_ids: tuple[int, ...],
    load_from: date,
    max_end: date,
) -> dict[int, list[dict[str, object]]]:
    """Published valuation rows per fund, one record per valuation day.

    A single query covers every product; the row-per-day dedup happens in
    Python on the (small) already-filtered result instead of issuing one
    query per fund.
    """

    rows = session.execute(
        select(
            ValuationVersion.fund_id,
            ValuationVersion.valuation_date,
            FundDailySnapshot.unit_nav,
            FundDailySnapshot.cumulative_unit_nav,
            FundDailySnapshot.cumulative_payout,
        )
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(
            ValuationVersion.fund_id.in_(fund_ids),
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            ValuationVersion.valuation_date >= load_from,
            ValuationVersion.valuation_date <= max_end,
        )
        .order_by(
            ValuationVersion.fund_id,
            ValuationVersion.valuation_date,
            ValuationVersion.id,
        )
    ).all()
    grouped: dict[int, list[dict[str, object]]] = {}
    index_by_day: dict[tuple[int, date], int] = {}
    for fund_id, valuation_day, unit_nav, cumulative_nav, payout in rows:
        bucket = grouped.setdefault(fund_id, [])
        record = {
            "valuation_date": valuation_day,
            "unit_nav": unit_nav,
            "cumulative_unit_nav": cumulative_nav,
            "cumulative_payout": payout,
        }
        key = (fund_id, valuation_day)
        if key in index_by_day:
            bucket[index_by_day[key]] = record
        else:
            index_by_day[key] = len(bucket)
            bucket.append(record)
    return grouped


def fund_range_drawdowns(
    session: Session,
    *,
    range_key: str,
    as_of: date | None,
    fund_ids: tuple[int, ...] | frozenset[int] | None = None,
) -> tuple[list[dict[str, object]], str | None]:
    """Per-product drawdown for one selected range.

    Each product is anchored to its own latest published valuation (or the
    exact ``as_of`` day when pinned), so a product that publishes on a
    different schedule gets its own real start and end dates instead of a
    company-wide window copied onto every row.
    """

    versions = _published_versions(session, as_of=as_of, fund_ids=fund_ids)
    if not versions:
        return [], None
    end_by_fund = {version.fund_id: version.valuation_date for version in versions}
    target_by_fund = {
        fund_id: calendar_start_date(end, range_key)
        for fund_id, end in end_by_fund.items()
    }
    max_end = max(end_by_fund.values())
    load_bounds = [
        (target - timedelta(days=BASELINE_LOOKBACK_DAYS))
        if (target := target_by_fund[fund_id]) is not None
        else (end - timedelta(days=FULL_WINDOW_DAYS))
        for fund_id, end in end_by_fund.items()
    ]
    load_from = min(load_bounds)
    fund_ids = tuple(sorted(end_by_fund))
    grouped = _published_nav_rows(session, fund_ids, load_from, max_end)
    names = {
        fund.id: fund.standard_name
        for fund in session.scalars(select(Fund).where(Fund.id.in_(fund_ids)))
    }
    results: list[dict[str, object]] = []
    for fund_id in fund_ids:
        result = calculate_range_drawdown(
            _adjusted_nav_points(grouped.get(fund_id, [])),
            start=target_by_fund[fund_id],
            end=end_by_fund[fund_id],
        )
        payload = _range_drawdown_payload(result)
        payload["fund_id"] = fund_id
        payload["name"] = names.get(fund_id)
        payload["valuation_date"] = end_by_fund[fund_id].isoformat()
        results.append(payload)
    note = (
        f"「全部」为已加载的近五年窗口（不早于 {load_from.isoformat()}）"
        if range_key == "all"
        else None
    )
    return results, note


@router.get("/api/v1/dashboard/fund-drawdowns")
def fund_drawdowns(
    context: CurrentContext,
    session: DatabaseSession,
    range_key: Annotated[
        str, Query(alias="range", pattern="^(1m|3m|ytd|1y|all)$")
    ] = "3m",
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    """Interval drawdown for every product row, computed in one batch."""

    funds, note = fund_range_drawdowns(
        session,
        range_key=range_key,
        as_of=as_of,
        fund_ids=visible_fund_ids(session, context.user),
    )
    available = sum(1 for fund in funds if fund["status"] in ("ok", "incomplete"))
    return {
        "data": {
            "range": range_key,
            "as_of": as_of.isoformat() if as_of else None,
            "methodology": RANGE_METHODOLOGY,
            "window_note": note,
            "funds": funds,
        },
        "meta": {
            "coverage": {"available": available, "total": len(funds)},
            "range": range_key,
            "as_of": as_of.isoformat() if as_of else None,
        },
    }


def _overview_funds(
    versions: list[ValuationVersion], views: dict[int, _VersionView]
) -> list[dict[str, object]]:
    data: list[dict[str, object]] = []
    for version in versions:
        view = views[version.id]
        snapshot = view.snapshot
        metric = view.metric
        data.append(
            {
                "id": version.fund_id,
                "name": view.fund.standard_name,
                "valuation_date": version.valuation_date.isoformat(),
                "total_assets": _decimal(snapshot.total_assets) if snapshot else None,
                "net_asset_value": _decimal(snapshot.net_asset_value)
                if snapshot
                else None,
                "unit_nav": _decimal(snapshot.unit_nav) if snapshot else None,
                "daily_return": _decimal(
                    metric.daily_return
                    if metric is not None
                    else snapshot.daily_return
                    if snapshot
                    else None
                ),
                "analysis_status": view.analysis_status,
                "analysis_run_id": view.analysis_run.id if view.analysis_run else None,
            }
        )
    return data


@router.get("/api/v1/funds")
def list_funds(
    context: CurrentContext,
    session: DatabaseSession,
    q: str | None = Query(default=None, max_length=255),
    status: FundStatus | None = Query(default=None),  # noqa: B008
    as_of: date | None = Query(default=None),  # noqa: B008
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    statement = select(Fund).order_by(Fund.standard_name, Fund.id)
    count_statement = select(func.count(Fund.id))
    allowed_ids = visible_fund_ids(session, context.user)
    if allowed_ids is not None:
        statement = statement.where(Fund.id.in_(allowed_ids))
        count_statement = count_statement.where(Fund.id.in_(allowed_ids))
    if q:
        filter_condition = Fund.standard_name.contains(q.strip())
        statement = statement.where(filter_condition)
        count_statement = count_statement.where(filter_condition)
    if status is not None:
        statement = statement.where(Fund.status == status)
        count_statement = count_statement.where(Fund.status == status)
    total = session.scalar(count_statement) or 0
    offset = (page - 1) * page_size
    data = []
    funds = list(session.scalars(statement.offset(offset).limit(page_size)))
    fund_ids = tuple(fund.id for fund in funds)
    versions = _published_versions(session, fund_ids=fund_ids, as_of=as_of)
    versions_by_fund = {version.fund_id: version for version in versions}
    views = _version_views(session, versions)
    for fund in funds:
        version = versions_by_fund.get(fund.id)
        view = views.get(version.id) if version else None
        snapshot = view.snapshot if view else None
        metric = view.metric if view else None
        data.append(
            {
                "id": fund.id,
                "name": fund.standard_name,
                "product_code": fund.product_code,
                "status": fund.status,
                "current_version_id": version.id if version else None,
                "valuation_date": version.valuation_date.isoformat()
                if version
                else None,
                "unit_nav": _decimal(snapshot.unit_nav) if snapshot else None,
                "daily_return": _decimal(
                    metric.daily_return
                    if metric is not None
                    else snapshot.daily_return
                    if snapshot
                    else None
                ),
                "quality_status": view.quality_status if view else "pending",
                "analysis_status": view.analysis_status if view else "pending",
                "analysis_run_id": view.analysis_run.id
                if view and view.analysis_run
                else None,
            }
        )
    return {
        "data": data,
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


def _benchmark_period_returns(
    session: Session,
    valuation_date: date,
    establishment_date: date | None,
    fund_cumulative_return: Decimal | None,
) -> dict[str, dict[str, object | None]]:
    """Compute benchmark returns for standard comparison periods.

    Each period is anchored to the fund's actual valuation date as the end
    point.  Start dates follow the same calendar-boundary rules as the
    existing fund return calculations (week/month/year boundaries).
    """

    from calendar import monthrange

    end = valuation_date
    periods: dict[str, date | None] = {
        "1w": end - timedelta(days=7),
        "1m": None,
        "3m": None,
        "1y": None,
        "inception": establishment_date,
    }
    # Month-based periods with clamped day-of-month.
    for label, months_back in [("1m", 1), ("3m", 3), ("1y", 12)]:
        y = end.year
        m = end.month - months_back
        while m <= 0:
            y -= 1
            m += 12
        max_day = monthrange(y, m)[1]
        periods[label] = date(y, m, min(end.day, max_day))

    # For inception, fall back to a very early date if no establishment_date.
    periods["inception"] = periods["inception"] or date(2000, 1, 1)

    # Fetch benchmark data covering the widest needed range.
    all_starts = [d for d in periods.values() if d is not None]
    if not all_starts:
        return {k: _empty_benchmark_return("no_period_start") for k in periods}

    range_start = min(all_starts)
    benchmark_rows = benchmark_series_for_range(
        session, range_start, end, BENCHMARK_TS_CODE
    )
    aligned = align_benchmark_to_dates(benchmark_rows, [range_start, end])

    # Also align each individual period start date.
    period_starts = [d for d in periods.values() if d is not None]
    aligned.update(align_benchmark_to_dates(benchmark_rows, period_starts))

    result: dict[str, dict[str, object | None]] = {}
    for label, start in periods.items():
        if start is None:
            result[label] = _empty_benchmark_return("no_period_start")
            continue

        ret, reason = benchmark_return(aligned, start, end)
        start_row = aligned.get(start)
        end_row = aligned.get(end)
        result[label] = {
            "benchmark_return": _decimal(ret),
            "benchmark_start_date": start.isoformat(),
            "benchmark_end_date": end.isoformat(),
            "benchmark_start_close": _decimal(start_row.close) if start_row else None,
            "benchmark_end_close": _decimal(end_row.close) if end_row else None,
            "unavailable_reason": reason,
        }

    # For inception, also populate fund_return so the frontend can show both.
    result["inception"]["fund_return"] = _decimal(fund_cumulative_return)

    return result


#: How far the first published valuation may sit after the establishment date
#: before the "since inception" window is declared incomplete.
INCEPTION_GAP_TOLERANCE_DAYS = 31


def _inception_drawdown(
    session: Session, fund: Fund, version: ValuationVersion
) -> dict[str, object]:
    """Since-inception drawdown over the product's complete published path.

    The loaded five-year chart window is not enough for this column: a
    product older than the window would otherwise report its five-year
    drawdown as if it were the since-inception figure. The whole published
    history is read here in one bounded query, and an incomplete history is
    reported as unavailable instead of being approximated from two endpoints.
    """

    end = version.valuation_date
    establishment = fund.establishment_date
    if establishment is None:
        return _unavailable_drawdown(
            "未记录成立日期，无法确认成立以来区间是否完整", end=end
        )
    rows = session.execute(
        select(
            ValuationVersion.valuation_date,
            FundDailySnapshot.unit_nav,
            FundDailySnapshot.cumulative_unit_nav,
            FundDailySnapshot.cumulative_payout,
        )
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(
            ValuationVersion.fund_id == fund.id,
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            ValuationVersion.valuation_date <= end,
        )
        .order_by(ValuationVersion.valuation_date, ValuationVersion.id)
    ).all()
    if not rows:
        return _unavailable_drawdown(
            "没有可用的历史估值记录", target_start=establishment, end=end
        )
    records: list[dict[str, object]] = []
    index_by_day: dict[date, int] = {}
    for valuation_day, unit_nav, cumulative_nav, payout in rows:
        record = {
            "valuation_date": valuation_day,
            "unit_nav": unit_nav,
            "cumulative_unit_nav": cumulative_nav,
            "cumulative_payout": payout,
        }
        if valuation_day in index_by_day:
            records[index_by_day[valuation_day]] = record
        else:
            index_by_day[valuation_day] = len(records)
            records.append(record)
    earliest = cast(date, records[0]["valuation_date"])
    if (
        earliest > establishment
        and (earliest - establishment).days > INCEPTION_GAP_TOLERANCE_DAYS
    ):
        return _unavailable_drawdown(
            "成立以来历史不完整："
            f"最早估值日 {earliest.isoformat()}，晚于成立日 {establishment.isoformat()}",
            target_start=establishment,
            end=end,
        )
    target_start = establishment if earliest <= establishment else None
    result = calculate_range_drawdown(
        _adjusted_nav_points(records), start=target_start, end=end
    )
    payload = _range_drawdown_payload(result)
    if target_start is None:
        payload["note"] = f"成立以来自最早可用估值日 {earliest.isoformat()} 起算"
    return payload


def _empty_benchmark_return(reason: str) -> dict[str, object | None]:
    return {
        "benchmark_return": None,
        "benchmark_start_date": None,
        "benchmark_end_date": None,
        "benchmark_start_close": None,
        "benchmark_end_close": None,
        "unavailable_reason": reason,
    }


@router.get("/api/v1/funds/{fund_id}")
def fund_detail(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    fund = session.get(Fund, fund_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    version = _version_for_fund(session, fund_id, as_of)
    analysis_status = _analysis_status(session, version) if version else "pending"
    analysis_run = (
        _analysis_run_for_version(session, version)
        if version and analysis_status == "ready"
        else None
    )
    snapshot = _snapshot(session, version.id) if version else None
    metric = _metric_for_version(session, version) if version else None
    aliases = session.scalars(
        select(FundAlias)
        .where(FundAlias.fund_id == fund.id)
        .order_by(FundAlias.match_priority.desc(), FundAlias.id)
    ).all()
    share_classes = session.scalars(
        select(ShareClass)
        .where(ShareClass.fund_id == fund.id)
        .order_by(ShareClass.share_code, ShareClass.id)
    ).all()
    return {
        "data": {
            "id": fund.id,
            "name": fund.standard_name,
            "product_code": fund.product_code,
            "strategy": fund.strategy,
            "manager": fund.manager,
            "establishment_date": fund.establishment_date,
            "notes": fund.notes,
            "aliases": [
                {
                    "id": alias.id,
                    "alias": alias.alias,
                    "source_location": alias.source_location,
                    "match_priority": alias.match_priority,
                    "valid_from": alias.valid_from,
                    "valid_to": alias.valid_to,
                }
                for alias in aliases
            ],
            "share_classes": [
                {
                    "id": share_class.id,
                    "share_code": share_class.share_code,
                    "share_name": share_class.share_name,
                    "enabled_from": share_class.enabled_from,
                    "disabled_from": share_class.disabled_from,
                    "status": ("inactive" if share_class.disabled_from else "active"),
                    "notes": share_class.notes,
                }
                for share_class in share_classes
            ],
            "status": fund.status,
            "current_version_id": version.id if version else None,
            "valuation_date": version.valuation_date.isoformat() if version else None,
            "quality_status": _quality_status(session, version.id)
            if version
            else "pending",
            "analysis_status": analysis_status,
            "analysis_run_id": analysis_run.id if analysis_run else None,
            "total_assets": _decimal(snapshot.total_assets) if snapshot else None,
            "net_asset_value": _decimal(snapshot.net_asset_value) if snapshot else None,
            "unit_nav": _decimal(snapshot.unit_nav) if snapshot else None,
            "cumulative_unit_nav": _decimal(snapshot.cumulative_unit_nav)
            if snapshot
            else None,
            "daily_return": _decimal(
                metric.daily_return
                if metric
                else snapshot.daily_return
                if snapshot
                else None
            ),
            "cumulative_return": _decimal(metric.cumulative_return) if metric else None,
            "inception_drawdown": _inception_drawdown(session, fund, version)
            if version
            else None,
            "period_returns": {
                "daily": _decimal(snapshot.daily_return) if snapshot else None,
                "wtd": _decimal(snapshot.wtd_return) if snapshot else None,
                "mtd": _decimal(snapshot.mtd_return) if snapshot else None,
                "qtd": _decimal(snapshot.qtd_return) if snapshot else None,
                "ytd": _decimal(snapshot.ytd_return) if snapshot else None,
                "cumulative": _decimal(snapshot.cumulative_return)
                if snapshot and snapshot.cumulative_return is not None
                else _decimal(metric.cumulative_return)
                if metric
                else None,
                "benchmark_returns": _benchmark_period_returns(
                    session,
                    version.valuation_date,
                    fund.establishment_date,
                    snapshot.cumulative_return
                    if snapshot and snapshot.cumulative_return is not None
                    else metric.cumulative_return
                    if metric
                    else None,
                )
                if version
                else None,
            },
        }
    }


@router.get("/api/v1/funds/{fund_id}/nav-series")
def nav_series(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    start: date | None = Query(default=None),  # noqa: B008
    end: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    if session.get(Fund, fund_id) is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    # Resolve one-sided windows before validating so every query is bounded.
    effective_end = end or datetime.now(UTC).date()
    lookback = 365 * 5 if end is not None else 365
    effective_start = start or date.fromordinal(
        max(date.min.toordinal(), effective_end.toordinal() - lookback)
    )
    if effective_end < effective_start:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if (effective_end - effective_start).days > 365 * 5:
        raise HTTPException(
            status_code=422,
            detail="窗口跨度不能超过 5 年，请缩小范围或使用导出功能",
        )
    statement = (
        select(ValuationVersion, FundDailySnapshot)
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(
            ValuationVersion.fund_id == fund_id,
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            ValuationVersion.valuation_date >= effective_start,
            ValuationVersion.valuation_date <= effective_end,
        )
        .order_by(ValuationVersion.valuation_date)
    )
    rows = list(session.execute(statement))
    versions = [version for version, _ in rows]
    views = _version_views(session, versions)
    records = [
        {
            "valuation_date": version.valuation_date,
            "unit_nav": snapshot.unit_nav,
            "cumulative_unit_nav": snapshot.cumulative_unit_nav,
            "cumulative_payout": snapshot.cumulative_payout,
        }
        for version, snapshot in rows
    ]
    result = calculate_nav_series(records)
    views_by_date = {version.valuation_date: views[version.id] for version in versions}
    points: list[dict[str, object]] = []
    sources: set[str] = set()
    for point in result.points:
        view = views_by_date.get(point.valuation_date)
        analysis_status = view.analysis_status if view else "pending"
        metric = view.metric if view else None
        metric_source = "persisted" if metric is not None else "calculated_fallback"
        sources.add(metric_source)
        points.append(
            {
                "valuation_date": point.valuation_date.isoformat(),
                "unit_nav": _decimal(point.unit_nav),
                "cumulative_unit_nav": _decimal(point.cumulative_unit_nav),
                "cumulative_payout": _decimal(point.cumulative_payout),
                "adjusted_nav": _decimal(point.adjusted_nav),
                "daily_return": _decimal(
                    metric.daily_return if metric is not None else point.daily_return
                ),
                "cumulative_return": _decimal(
                    metric.cumulative_return
                    if metric is not None
                    else point.cumulative_return
                ),
                "analysis_status": analysis_status,
                "analysis_run_id": (
                    metric.source_analysis_run_id if metric is not None else None
                ),
                "metric_source": metric_source,
            }
        )
    overall_status = (
        "stale"
        if any(view.analysis_status == "stale" for view in views.values())
        else "pending"
        if any(view.analysis_status == "pending" for view in views.values())
        or not views
        else "ready"
    )
    metric_source = (
        "none"
        if not sources
        else "persisted"
        if sources == {"persisted"}
        else "calculated_fallback"
        if sources == {"calculated_fallback"}
        else "mixed"
    )
    return {
        "data": {
            "methodology": result.methodology,
            "total_return": _decimal(result.total_return),
            "points": points,
            "benchmark": _benchmark_data(session, effective_start, effective_end),
        },
        "meta": {
            "coverage": {"available": len(records), "total": len(records)},
            "analysis_status": overall_status,
            "metric_source": metric_source,
        },
    }


@router.get("/api/v1/funds/{fund_id}/positions")
def positions(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    if session.get(Fund, fund_id) is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    version = _version_for_fund(session, fund_id, as_of)
    if version is None:
        return {"data": [], "meta": {"page": page, "page_size": page_size, "total": 0}}
    statement = (
        select(PositionDaily)
        .where(PositionDaily.valuation_version_id == version.id)
        .order_by(PositionDaily.market_value.desc(), PositionDaily.id)
    )
    offset = (page - 1) * page_size
    total = (
        session.scalar(
            select(func.count(PositionDaily.id)).where(
                PositionDaily.valuation_version_id == version.id
            )
        )
        or 0
    )
    rows = session.scalars(statement.offset(offset).limit(page_size))
    return {
        "data": [
            {
                "security_code": row.security_code,
                "security_name": row.security_name,
                "market": row.market,
                "account": row.account,
                "quantity": _decimal(row.quantity),
                "market_price": _decimal(row.market_price),
                "market_value": _decimal(row.market_value),
                "nav_weight": _decimal(row.nav_weight),
                "suspension_info": row.suspension_info,
            }
            for row in rows
        ],
        "meta": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "valuation_date": version.valuation_date.isoformat(),
        },
    }


@router.get("/api/v1/funds/{fund_id}/quality")
def quality(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    if session.get(Fund, fund_id) is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    version = _version_for_fund(session, fund_id, as_of)
    if version is None:
        return {
            "data": {"version_id": None, "validation": [], "quality_status": "pending"}
        }
    findings = session.scalars(
        select(ValidationResult)
        .where(ValidationResult.valuation_version_id == version.id)
        .where(ValidationResult.ignored.is_(False))
        .order_by(ValidationResult.level, ValidationResult.id)
    ).all()
    return {
        "data": {
            "version_id": version.id,
            "valuation_date": version.valuation_date.isoformat(),
            "quality_status": _quality_status(session, version.id),
            "validation": [
                {
                    "rule_code": finding.rule_code,
                    "level": finding.level,
                    "actual_value": _decimal(finding.actual_value),
                    "expected_value": _decimal(finding.expected_value),
                    "difference": _decimal(finding.difference),
                    "source_location": finding.source_location,
                    "message": finding.message,
                }
                for finding in findings
            ],
        }
    }
