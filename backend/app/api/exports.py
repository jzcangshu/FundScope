"""Streaming CSV exports for visible published data."""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Annotated, Literal
from typing import cast as type_cast

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import String as SqlString
from sqlalchemy import and_, cast, false, func, select
from sqlalchemy.orm import Session

from app.analytics.drawdown import (
    RANGE_METHODOLOGY,
    calculate_range_drawdown,
    calendar_start_date,
)
from app.analytics.nav import calculate_nav_series
from app.api.dashboard import (
    BASELINE_LOOKBACK_DAYS,
    FULL_WINDOW_DAYS,
    fund_range_drawdowns,
)
from app.auth.dependencies import AuthContext, get_auth_context, get_db, require_roles
from app.auth.fund_access import require_fund_access, visible_fund_ids
from app.auth.service import AuthService
from app.db.base import (
    AuditResult,
    FundStatus,
    ImportBatchStatus,
    SourceType,
    UserRole,
    ValuationStatus,
)
from app.db.models import (
    AccountSubjectDaily,
    BackgroundJob,
    Fund,
    FundDailySnapshot,
    ImportBatch,
    PositionDaily,
    ShareClass,
    ShareClassDailySnapshot,
    ValuationVersion,
)
from app.imports.access import batch_access_clause

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])

DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[AuthContext, Depends(get_auth_context)]
ImportReader = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]
Denominator = Literal["net_asset_value", "total_assets", "market_value"]
STREAM_BATCH_SIZE = 200

CSVHeaders = tuple[str, ...]
RowFactory = Callable[[], Iterator[Iterable[object]]]


def _cell(value: object) -> str:
    """Convert a value to CSV text and neutralize spreadsheet formulas."""

    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        value = value.value
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _csv_response(
    *,
    filename: str,
    headers: CSVHeaders,
    rows: RowFactory,
    data_as_of: str,
    extra_response_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    exported_at = datetime.now(UTC).isoformat()

    def content() -> Iterator[bytes]:
        buffer = StringIO(newline="")
        csv_writer = csv.writer(buffer, lineterminator="\r\n")
        yield b"\xef\xbb\xbf"
        csv_writer.writerow(headers)
        yield buffer.getvalue().encode("utf-8")
        for row in rows():
            buffer.seek(0)
            buffer.truncate(0)
            csv_writer.writerow([_cell(value) for value in row])
            yield buffer.getvalue().encode("utf-8")

    return StreamingResponse(
        content(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-At": exported_at,
            "X-Data-As-Of": data_as_of,
            **(extra_response_headers or {}),
        },
    )


def _audit_export(
    session: Session,
    context: AuthContext,
    *,
    action: str,
    summary: dict[str, object],
) -> None:
    AuthService(session).record_audit(
        action=action,
        resource_type="export",
        resource_id=action.removeprefix("export."),
        actor_user_id=context.user.id,
        summary={"format": "csv", **summary},
        result=AuditResult.SUCCESS,
    )
    session.commit()


def _published_versions_statement(
    *,
    fund_id: int | None = None,
    as_of: date | None = None,
    query: str | None = None,
):
    statement = (
        select(Fund, ValuationVersion, FundDailySnapshot)
        .join(
            ValuationVersion,
            and_(
                ValuationVersion.fund_id == Fund.id,
                ValuationVersion.status == ValuationStatus.PUBLISHED,
            ),
        )
        .outerjoin(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(Fund.status == FundStatus.ACTIVE)
    )
    if fund_id is not None:
        statement = statement.where(Fund.id == fund_id)
    if query:
        statement = statement.where(Fund.standard_name.contains(query.strip()))
    if as_of is not None:
        statement = statement.where(ValuationVersion.valuation_date == as_of)
    else:
        latest = (
            select(
                ValuationVersion.fund_id,
                func.max(ValuationVersion.valuation_date).label("latest_date"),
            )
            .where(ValuationVersion.status == ValuationStatus.PUBLISHED)
            .group_by(ValuationVersion.fund_id)
            .subquery()
        )
        statement = statement.join(
            latest,
            and_(
                latest.c.fund_id == ValuationVersion.fund_id,
                latest.c.latest_date == ValuationVersion.valuation_date,
            ),
        )
    return statement.order_by(
        ValuationVersion.valuation_date.desc(), Fund.id, ValuationVersion.id
    )


def _version_for_fund(
    session: Session, fund_id: int, as_of: date | None
) -> ValuationVersion | None:
    statement = select(ValuationVersion).join(
        Fund,
        and_(
            Fund.id == ValuationVersion.fund_id,
            Fund.status == FundStatus.ACTIVE,
        ),
    )
    statement = statement.where(
        ValuationVersion.fund_id == fund_id,
        ValuationVersion.status == ValuationStatus.PUBLISHED,
    )
    if as_of is not None:
        statement = statement.where(ValuationVersion.valuation_date == as_of)
    return session.scalar(
        statement.order_by(
            ValuationVersion.valuation_date.desc(), ValuationVersion.id.desc()
        ).limit(1)
    )


def _exportable_fund(session: Session, fund_id: int) -> Fund:
    fund = session.get(Fund, fund_id)
    if fund is None or fund.status != FundStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="Fund not found")
    return fund


def _as_of_value(as_of: date | None) -> str:
    return as_of.isoformat() if as_of is not None else "latest"


def _as_decimal(value: object) -> Decimal | None:
    """Drawdown payloads travel as strings; convert back for CSV formatting."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


#: Ratio precision for exported drawdown columns; calculation itself is exact.
DRAWDOWN_EXPORT_DP = Decimal("0.000001")


def _drawdown_cell(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(DRAWDOWN_EXPORT_DP)


@router.get("/overview")
def export_overview(
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
    range_key: Annotated[
        str | None,
        Query(alias="range", pattern="^(1m|3m|ytd|1y|all)$"),
    ] = None,
) -> StreamingResponse:
    allowed_ids = visible_fund_ids(session, context.user)
    statement = _published_versions_statement(as_of=as_of)
    if allowed_ids is not None:
        statement = statement.where(Fund.id.in_(allowed_ids))
    # One batched calculation for every product row, not one query per fund.
    drawdown_by_fund: dict[int, dict[str, object]] = {}
    if range_key:
        funds, _ = fund_range_drawdowns(
            session, range_key=range_key, as_of=as_of, fund_ids=allowed_ids
        )
        drawdown_by_fund = {
            type_cast(int, fund["fund_id"]): fund
            for fund in funds
            if fund["fund_id"] is not None
        }
    _audit_export(
        session,
        context,
        action="export.overview",
        summary={"as_of": _as_of_value(as_of), "range": range_key},
    )

    def rows() -> Iterator[Iterable[object]]:
        for fund, version, snapshot in session.execute(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            drawdown = drawdown_by_fund.get(fund.id)
            yield (
                fund.product_code,
                fund.standard_name,
                version.valuation_date,
                snapshot.net_asset_value if snapshot else None,
                snapshot.unit_nav if snapshot else None,
                snapshot.daily_return if snapshot else None,
                drawdown.get("start") if drawdown else None,
                _drawdown_cell(_as_decimal(drawdown.get("current_drawdown")))
                if drawdown
                else None,
                _drawdown_cell(_as_decimal(drawdown.get("max_drawdown")))
                if drawdown
                else None,
            )

    return _csv_response(
        filename="company-overview.csv",
        headers=(
            "产品编号",
            "产品名称",
            "估值日",
            "净资产",
            "单位净值",
            "日收益",
            "区间起点",
            "区间当前回撤",
            "区间最大回撤",
        ),
        rows=rows,
        data_as_of=_as_of_value(as_of),
        extra_response_headers={
            "X-Drawdown-Range": range_key or "",
            "X-Drawdown-Caliber": RANGE_METHODOLOGY if range_key else "",
        },
    )


@router.get("/funds")
def export_funds(
    context: CurrentContext,
    session: DatabaseSession,
    q: str | None = Query(default=None, max_length=255),
    as_of: date | None = Query(default=None),  # noqa: B008
) -> StreamingResponse:
    statement = _published_versions_statement(query=q, as_of=as_of)
    allowed_ids = visible_fund_ids(session, context.user)
    if allowed_ids is not None:
        statement = statement.where(Fund.id.in_(allowed_ids))
    _audit_export(
        session,
        context,
        action="export.funds",
        summary={"query": q.strip() if q else None, "as_of": _as_of_value(as_of)},
    )

    def rows() -> Iterator[Iterable[object]]:
        for fund, version, snapshot in session.execute(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            yield (
                fund.product_code,
                fund.standard_name,
                fund.status,
                version.valuation_date,
                snapshot.unit_nav if snapshot else None,
                snapshot.daily_return if snapshot else None,
            )

    return _csv_response(
        filename="funds.csv",
        headers=(
            "产品编号",
            "产品名称",
            "产品状态",
            "估值日",
            "单位净值",
            "日收益",
        ),
        rows=rows,
        data_as_of=_as_of_value(as_of),
    )


@router.get("/funds/{fund_id}/overview")
def export_fund_overview(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> StreamingResponse:
    require_fund_access(session, context.user, fund_id)
    _exportable_fund(session, fund_id)
    statement = _published_versions_statement(fund_id=fund_id, as_of=as_of)
    _audit_export(
        session,
        context,
        action="export.fund_overview",
        summary={"fund_id": fund_id, "as_of": _as_of_value(as_of)},
    )

    def rows() -> Iterator[Iterable[object]]:
        for fund, version, snapshot in session.execute(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            yield (
                fund.product_code,
                fund.standard_name,
                version.valuation_date,
                version.version_no,
                snapshot.total_assets if snapshot else None,
                snapshot.total_liabilities if snapshot else None,
                snapshot.net_asset_value if snapshot else None,
                snapshot.unit_nav if snapshot else None,
                snapshot.cumulative_unit_nav if snapshot else None,
                snapshot.daily_return if snapshot else None,
                snapshot.cumulative_return if snapshot else None,
            )

    return _csv_response(
        filename=f"fund-{fund_id}-overview.csv",
        headers=(
            "产品编号",
            "产品名称",
            "估值日",
            "版本号",
            "总资产",
            "总负债",
            "净资产",
            "单位净值",
            "累计单位净值",
            "日收益",
            "累计收益",
        ),
        rows=rows,
        data_as_of=_as_of_value(as_of),
    )


@router.get("/funds/{fund_id}/nav-series")
def export_nav_series(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    start: date | None = Query(default=None),  # noqa: B008
    end: date | None = Query(default=None),  # noqa: B008
    range_key: Annotated[
        str | None,
        Query(alias="range", pattern="^(1m|3m|ytd|1y|all)$"),
    ] = None,
) -> StreamingResponse:
    require_fund_access(session, context.user, fund_id)
    _exportable_fund(session, fund_id)
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    filters = [
        ValuationVersion.fund_id == fund_id,
        ValuationVersion.status == ValuationStatus.PUBLISHED,
    ]
    if start is not None:
        filters.append(ValuationVersion.valuation_date >= start)
    elif end is None:
        # Cap default lookback to one year so a long-running fund does not
        # stream an unbounded history; clients can still pass start/end.
        filters.append(
            ValuationVersion.valuation_date
            >= datetime.now(UTC).date() - timedelta(days=365)
        )
    if end is not None:
        filters.append(ValuationVersion.valuation_date <= end)
    # Same caliber as the on-screen table: resolve the window first, then
    # recompute the standard drawdown inside it.
    drawdown_by_date: dict[date, Decimal | None] = {}
    drawdown_status = "not_requested"
    drawdown_start: date | None = None
    if range_key:
        effective_end = end or session.scalar(
            select(func.max(ValuationVersion.valuation_date)).where(*filters)
        )
        if effective_end is None:
            drawdown_status = "insufficient"
        else:
            cutoff = (
                None
                if range_key == "all"
                else calendar_start_date(effective_end, range_key)
            )
            lookback = (
                effective_end - timedelta(days=FULL_WINDOW_DAYS)
                if cutoff is None
                else cutoff - timedelta(days=BASELINE_LOOKBACK_DAYS)
            )
            records = [
                {
                    "valuation_date": valuation_day,
                    "unit_nav": unit_nav,
                    "cumulative_unit_nav": cumulative_nav,
                    "cumulative_payout": payout,
                }
                for valuation_day, unit_nav, cumulative_nav, payout in session.execute(
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
                        ValuationVersion.fund_id == fund_id,
                        ValuationVersion.status == ValuationStatus.PUBLISHED,
                        ValuationVersion.valuation_date >= lookback,
                        ValuationVersion.valuation_date <= effective_end,
                    )
                    .order_by(ValuationVersion.valuation_date, ValuationVersion.id)
                )
            ]
            series = calculate_nav_series(records)
            result = calculate_range_drawdown(
                [
                    {
                        "valuation_date": point.valuation_date,
                        "adjusted_nav": point.adjusted_nav,
                    }
                    for point in series.points
                ],
                start=cutoff,
                end=effective_end,
            )
            drawdown_status = result.status
            drawdown_start = result.start
            drawdown_by_date = {
                point.valuation_date: point.drawdown for point in result.points
            }
            # Keep the CSV rows aligned with the window actually shown.
            if result.start is not None:
                filters.append(ValuationVersion.valuation_date >= result.start)
            if end is None:
                filters.append(ValuationVersion.valuation_date <= effective_end)
    nav_join = select(FundDailySnapshot).join(
        ValuationVersion,
        FundDailySnapshot.valuation_version_id == ValuationVersion.id,
    )
    total = session.scalar(select(func.count(ValuationVersion.id)).where(*filters)) or 0
    cumulative_count = (
        session.scalar(
            nav_join.with_only_columns(
                func.count(FundDailySnapshot.cumulative_unit_nav)
            ).where(*filters)
        )
        or 0
    )
    payout_count = (
        session.scalar(
            nav_join.with_only_columns(
                func.count(FundDailySnapshot.cumulative_payout)
            ).where(*filters)
        )
        or 0
    )
    use_cumulative = total > 0 and cumulative_count == total
    has_payout = payout_count > 0
    _audit_export(
        session,
        context,
        action="export.nav_series",
        summary={
            "fund_id": fund_id,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
    )

    statement = (
        select(ValuationVersion, FundDailySnapshot)
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(*filters)
        .order_by(ValuationVersion.valuation_date, ValuationVersion.id)
    )

    def rows() -> Iterator[Iterable[object]]:
        baseline: Decimal | None = None
        previous: Decimal | None = None
        methodology = (
            "cumulative_unit_nav"
            if use_cumulative
            else "unit_nav_plus_cumulative_payout"
            if has_payout
            else "unit_nav"
        )
        for version, snapshot in session.execute(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            if use_cumulative:
                adjusted = snapshot.cumulative_unit_nav
            else:
                unit_nav = snapshot.unit_nav
                payout = snapshot.cumulative_payout
                adjusted = (
                    None
                    if unit_nav is None or (has_payout and payout is None)
                    else unit_nav + (payout or Decimal(0))
                )
            daily_return = (
                adjusted / previous - Decimal(1)
                if adjusted is not None and previous not in (None, Decimal(0))
                else None
            )
            if baseline is None and adjusted is not None:
                baseline = adjusted
            cumulative_return = (
                adjusted / baseline - Decimal(1)
                if adjusted is not None and baseline not in (None, Decimal(0))
                else None
            )
            yield (
                version.valuation_date,
                snapshot.unit_nav,
                snapshot.cumulative_unit_nav,
                snapshot.cumulative_payout,
                adjusted,
                daily_return,
                cumulative_return,
                _drawdown_cell(drawdown_by_date.get(version.valuation_date)),
                methodology,
            )
            previous = adjusted

    return _csv_response(
        filename=f"fund-{fund_id}-nav-series.csv",
        headers=(
            "日期",
            "单位净值",
            "累计单位净值",
            "累计分红",
            "调整后净值",
            "日收益",
            "累计收益",
            "区间回撤",
            "口径",
        ),
        rows=rows,
        data_as_of=f"{start.isoformat() if start else ''}/{end.isoformat() if end else ''}",
        extra_response_headers={
            "X-Drawdown-Range": range_key or "",
            "X-Drawdown-Caliber": RANGE_METHODOLOGY if range_key else "",
            "X-Drawdown-Status": drawdown_status,
            "X-Drawdown-Start": drawdown_start.isoformat() if drawdown_start else "",
        },
    )


@router.get("/funds/{fund_id}/allocation")
def export_allocation(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
    denominator: Denominator = Query(default="net_asset_value"),  # noqa: B008
) -> StreamingResponse:
    require_fund_access(session, context.user, fund_id)
    _exportable_fund(session, fund_id)
    version = _version_for_fund(session, fund_id, as_of)
    snapshot = (
        session.scalar(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id == version.id
            )
        )
        if version
        else None
    )
    if denominator == "net_asset_value":
        divisor = snapshot.net_asset_value if snapshot else None
    elif denominator == "total_assets":
        divisor = snapshot.total_assets if snapshot else None
    else:
        divisor = None
    allocation_filters = (
        [
            AccountSubjectDaily.valuation_version_id == version.id,
            AccountSubjectDaily.is_leaf.is_(True),
            AccountSubjectDaily.include_in_holdings.is_(True),
            AccountSubjectDaily.standard_category.is_not(None),
            AccountSubjectDaily.market_value.is_not(None),
        ]
        if version
        else [false()]
    )
    grouped = (
        select(
            AccountSubjectDaily.standard_category,
            func.sum(AccountSubjectDaily.market_value).label("market_value"),
        )
        .where(
            *allocation_filters,
        )
        .group_by(AccountSubjectDaily.standard_category)
        .order_by(func.abs(func.sum(AccountSubjectDaily.market_value)).desc())
    )
    if denominator == "market_value":
        divisor = session.scalar(
            select(func.sum(AccountSubjectDaily.market_value)).where(
                *allocation_filters,
            )
        )
    _audit_export(
        session,
        context,
        action="export.allocation",
        summary={
            "fund_id": fund_id,
            "as_of": _as_of_value(as_of),
            "denominator": denominator,
        },
    )

    def rows() -> Iterator[Iterable[object]]:
        for category, market_value in session.execute(
            grouped.execution_options(stream_results=True, yield_per=STREAM_BATCH_SIZE)
        ):
            weight = (
                market_value / divisor if divisor not in (None, Decimal(0)) else None
            )
            yield category, market_value, weight

    return _csv_response(
        filename=f"fund-{fund_id}-allocation.csv",
        headers=("资产类别", "市值", "权重"),
        rows=rows,
        data_as_of=_as_of_value(as_of),
    )


@router.get("/funds/{fund_id}/positions")
def export_positions(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
    account: str | None = Query(default=None, max_length=255),
    market: str | None = Query(default=None, max_length=100),
) -> StreamingResponse:
    require_fund_access(session, context.user, fund_id)
    _exportable_fund(session, fund_id)
    version = _version_for_fund(session, fund_id, as_of)
    filters = [
        PositionDaily.valuation_version_id == version.id if version else false(),
    ]
    if account:
        filters.append(PositionDaily.account == account.strip())
    if market:
        filters.append(PositionDaily.market == market.strip())
    statement = (
        select(PositionDaily)
        .where(*filters)
        .order_by(PositionDaily.market_value.desc(), PositionDaily.id)
    )
    _audit_export(
        session,
        context,
        action="export.positions",
        summary={
            "fund_id": fund_id,
            "as_of": _as_of_value(as_of),
            "account": account.strip() if account else None,
            "market": market.strip() if market else None,
        },
    )

    def rows() -> Iterator[Iterable[object]]:
        for position in session.scalars(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            yield (
                position.security_code,
                position.security_name,
                position.market,
                position.account,
                position.quantity,
                position.unit_cost,
                position.cost,
                position.market_price,
                position.market_value,
                position.nav_weight,
                position.valuation_gain,
                position.suspension_info,
            )

    return _csv_response(
        filename=f"fund-{fund_id}-positions.csv",
        headers=(
            "证券代码",
            "证券名称",
            "市场",
            "账户",
            "数量",
            "单位成本",
            "成本",
            "市价",
            "市值",
            "净值权重",
            "估值增值",
            "停牌信息",
        ),
        rows=rows,
        data_as_of=_as_of_value(as_of),
    )


@router.get("/funds/{fund_id}/share-classes")
def export_share_classes(
    fund_id: int,
    context: CurrentContext,
    session: DatabaseSession,
    as_of: date | None = Query(default=None),  # noqa: B008
) -> StreamingResponse:
    require_fund_access(session, context.user, fund_id)
    _exportable_fund(session, fund_id)
    version = _version_for_fund(session, fund_id, as_of)
    statement = (
        select(ShareClass, ShareClassDailySnapshot)
        .join(
            ShareClassDailySnapshot,
            ShareClassDailySnapshot.share_class_id == ShareClass.id,
        )
        .where(
            ShareClass.fund_id == fund_id,
            ShareClassDailySnapshot.valuation_version_id == version.id
            if version
            else false(),
        )
        .order_by(ShareClass.share_code, ShareClass.id)
    )
    _audit_export(
        session,
        context,
        action="export.share_classes",
        summary={"fund_id": fund_id, "as_of": _as_of_value(as_of)},
    )

    def rows() -> Iterator[Iterable[object]]:
        for share_class, snapshot in session.execute(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            yield (
                share_class.share_code,
                share_class.share_name,
                snapshot.net_assets,
                snapshot.paid_in_capital,
                snapshot.unit_nav,
                snapshot.cumulative_unit_nav,
                snapshot.daily_return,
                snapshot.ytd_return,
                snapshot.mtd_return,
                snapshot.qtd_return,
                snapshot.wtd_return,
            )

    return _csv_response(
        filename=f"fund-{fund_id}-share-classes.csv",
        headers=(
            "份额代码",
            "份额名称",
            "净资产",
            "实收资本",
            "单位净值",
            "累计单位净值",
            "日收益",
            "年初收益",
            "月初收益",
            "季初收益",
            "周初收益",
        ),
        rows=rows,
        data_as_of=_as_of_value(as_of),
    )


@router.get("/imports")
def export_import_report(
    context: ImportReader,
    session: DatabaseSession,
    source_type: SourceType | None = Query(default=None),  # noqa: B008
    status: ImportBatchStatus | None = Query(default=None),  # noqa: B008
    start: date | None = Query(default=None),  # noqa: B008
    end: date | None = Query(default=None),  # noqa: B008
) -> StreamingResponse:
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="start must not be after end")
    statement = (
        select(ImportBatch, BackgroundJob)
        .outerjoin(
            BackgroundJob,
            and_(
                BackgroundJob.job_type == "process_import_batch",
                BackgroundJob.resource_id == cast(ImportBatch.id, SqlString),
            ),
        )
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
    )
    statement = statement.where(batch_access_clause(session, context.user))
    if source_type is not None:
        statement = statement.where(ImportBatch.source_type == source_type)
    if status is not None:
        statement = statement.where(ImportBatch.status == status)
    if start is not None:
        statement = statement.where(
            ImportBatch.created_at >= datetime.combine(start, time.min, tzinfo=UTC)
        )
    if end is not None:
        statement = statement.where(
            ImportBatch.created_at
            < datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        )
    _audit_export(
        session,
        context,
        action="export.imports",
        summary={
            "source_type": source_type.value if source_type else None,
            "status": status.value if status else None,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
        },
    )

    def rows() -> Iterator[Iterable[object]]:
        for batch, job in session.execute(
            statement.execution_options(
                stream_results=True, yield_per=STREAM_BATCH_SIZE
            )
        ):
            yield (
                batch.id,
                batch.source_type,
                batch.file_count,
                batch.status,
                job.status if job else None,
                batch.created_at,
            )

    return _csv_response(
        filename="import-report.csv",
        headers=("批次编号", "来源", "文件数", "批次状态", "任务状态", "创建时间"),
        rows=rows,
        data_as_of="",
    )


__all__ = ["router"]
