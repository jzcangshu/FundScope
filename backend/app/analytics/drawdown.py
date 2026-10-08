"""Running peak and drawdown calculations."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ._common import dated_records, decimal, field

# ---------------------------------------------------------------------------
# Range-scoped drawdown
# ---------------------------------------------------------------------------
# Standard formula, evaluated inside the selected window only:
#   Pt = max(Vs) for t0 <= s <= t   (running peak, reset at the window start)
#   Dt = Vt / Pt - 1                (0 at the first point, never positive)
#   max drawdown    = min(Dt) over the window
#   current drawdown = DT at the selected end (valuation) date
# The window start is the latest valuation on or before the requested natural
# day; highs observed before it never participate. Normalising the NAV series
# (to 1 or 100) does not change any of these numbers.

RANGE_METHODOLOGY = "standard_running_peak_in_range"

STATUS_OK = "ok"
STATUS_INCOMPLETE = "incomplete"
STATUS_INSUFFICIENT = "insufficient"
STATUS_INVALID = "invalid"

#: Window labels accepted by the range endpoints (mirrors the frontend tabs).
RANGE_KEYS = ("1m", "3m", "ytd", "1y", "all")
#: Fixed statistic columns of the product "收益与回撤" table.
PERIOD_KEYS = ("1w", "1m", "3m", "1y", "inception")

_MISSING_DATA_REASON = "区间内存在缺失或无效的净值，回撤仅基于已观测数据"


def calendar_start_date(end: date, period: str) -> date | None:
    """Natural-day window start for a period label, or None for "all".

    Month arithmetic clamps the day-of-month (2024-03-31 minus one month is
    2024-02-29), matching the frontend ``cutoffForRange`` / ``returnCutoff``.
    """

    if period == "all":
        return None
    if period == "1w":
        return end - timedelta(days=7)
    if period == "ytd":
        return date(end.year, 1, 1)
    months = {"1m": 1, "3m": 3, "1y": 12}.get(period)
    if months is None:
        raise ValueError(f"unsupported period: {period}")
    year = end.year
    month = end.month - months
    while month <= 0:
        year -= 1
        month += 12
    day = min(end.day, monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    valuation_date: date
    value: Decimal | None
    peak_value: Decimal | None
    drawdown: Decimal | None
    max_drawdown: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DrawdownResult:
    points: tuple[DrawdownPoint, ...]
    current_drawdown: Decimal | None
    max_drawdown: Decimal | None
    peak_date: date | None
    trough_date: date | None
    peak_value: Decimal | None
    trough_value: Decimal | None


def calculate_drawdown(records: list[Any] | tuple[Any, ...]) -> DrawdownResult:
    """Calculate drawdown from NAV points without filling missing values."""

    dated = dated_records(records)
    running_peak: Decimal | None = None
    running_peak_date: date | None = None
    worst: Decimal | None = None
    worst_peak_date: date | None = None
    worst_trough_date: date | None = None
    worst_peak_value: Decimal | None = None
    worst_trough_value: Decimal | None = None
    points: list[DrawdownPoint] = []

    for valuation_day, record in dated:
        value = decimal(field(record, "adjusted_nav", "cumulative_unit_nav", "value"))
        if value is None:
            points.append(DrawdownPoint(valuation_day, None, running_peak, None, worst))
            continue

        if running_peak is None or value > running_peak:
            running_peak = value
            running_peak_date = valuation_day

        drawdown = None if running_peak == 0 else value / running_peak - Decimal(1)
        if drawdown is not None and (worst is None or drawdown < worst):
            worst = drawdown
            worst_peak_date = running_peak_date
            worst_trough_date = valuation_day
            worst_peak_value = running_peak
            worst_trough_value = value
        points.append(
            DrawdownPoint(valuation_day, value, running_peak, drawdown, worst)
        )

    current_drawdown = points[-1].drawdown if points else None
    if worst is None and running_peak not in (None, Decimal(0)):
        worst = Decimal(0)
        worst_peak_date = next(
            point.valuation_date for point in points if point.value is not None
        )
        worst_trough_date = worst_peak_date
        worst_peak_value = running_peak
        worst_trough_value = running_peak
    return DrawdownResult(
        points=tuple(points),
        current_drawdown=current_drawdown,
        max_drawdown=worst,
        peak_date=worst_peak_date,
        trough_date=worst_trough_date,
        peak_value=worst_peak_value,
        trough_value=worst_trough_value,
    )


def calculate_max_drawdown(records: list[Any] | tuple[Any, ...]) -> DrawdownResult:
    return calculate_drawdown(records)


# ---------------------------------------------------------------------------
# Range-scoped drawdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RangeDrawdownPoint:
    valuation_date: date
    value: Decimal | None
    peak_value: Decimal | None
    drawdown: Decimal | None
    running_max_drawdown: Decimal | None


@dataclass(frozen=True, slots=True)
class RangeDrawdownResult:
    """Drawdown of one selected window, recomputed from that window's NAV."""

    methodology: str
    status: str
    reason: str | None
    target_start: date | None
    start: date | None
    end: date | None
    points: tuple[RangeDrawdownPoint, ...]
    current_drawdown: Decimal | None
    max_drawdown: Decimal | None
    peak_date: date | None
    trough_date: date | None
    peak_value: Decimal | None
    trough_value: Decimal | None
    observed_points: int
    missing_points: int


def _nav_value(record: Any) -> Decimal | None:
    """Adjusted NAV usable for peaks and drawdown, or None when invalid.

    Missing, zero, negative and non-finite values never participate in a peak
    or a ratio; they break the series instead of being coerced to a number.
    """

    try:
        value = decimal(field(record, "adjusted_nav", "cumulative_unit_nav", "value"))
    except (ValueError, TypeError):
        return None
    if value is None or not value.is_finite() or value <= 0:
        return None
    return value


def _range_result(
    *,
    status: str,
    reason: str | None,
    target_start: date | None,
    start: date | None = None,
    end: date | None = None,
    points: tuple[RangeDrawdownPoint, ...] = (),
    current_drawdown: Decimal | None = None,
    max_drawdown: Decimal | None = None,
    peak_date: date | None = None,
    trough_date: date | None = None,
    peak_value: Decimal | None = None,
    trough_value: Decimal | None = None,
    observed_points: int = 0,
    missing_points: int = 0,
) -> RangeDrawdownResult:
    return RangeDrawdownResult(
        methodology=RANGE_METHODOLOGY,
        status=status,
        reason=reason,
        target_start=target_start,
        start=start,
        end=end,
        points=points,
        current_drawdown=current_drawdown,
        max_drawdown=max_drawdown,
        peak_date=peak_date,
        trough_date=trough_date,
        peak_value=peak_value,
        trough_value=trough_value,
        observed_points=observed_points,
        missing_points=missing_points,
    )


def calculate_range_drawdown(
    records: list[Any] | tuple[Any, ...],
    *,
    start: date | None = None,
    end: date | None = None,
) -> RangeDrawdownResult:
    """Recompute the standard drawdown inside ``[start, end]``.

    ``start`` is the requested natural-day window start: the baseline is the
    latest valuation on or before it, and that baseline is included in the
    series. ``end`` is matched exactly; a valuation day without a published
    record yields an unavailable result instead of silently showing another
    day. Peaks reset at the baseline, so highs before the window and the
    residual peak of a wider query can never leak into the numbers.
    """

    dated = dated_records(records)
    if not dated:
        return _range_result(
            status=STATUS_INSUFFICIENT,
            reason="区间内没有可用的净值记录",
            target_start=start,
        )

    if end is None:
        end_index = len(dated) - 1
    else:
        end_index = next(
            (
                index
                for index in range(len(dated) - 1, -1, -1)
                if dated[index][0] == end
            ),
            None,
        )
        if end_index is None:
            return _range_result(
                status=STATUS_INSUFFICIENT,
                reason="该估值日没有已发布的净值记录",
                target_start=start,
                end=end,
            )

    if start is None:
        start_index = 0
    else:
        if dated[end_index][0] < start:
            return _range_result(
                status=STATUS_INSUFFICIENT,
                reason=(
                    "该产品在所选区间内没有估值记录"
                    f"（最新估值日 {dated[end_index][0].isoformat()}）"
                ),
                target_start=start,
                end=dated[end_index][0],
            )
        start_index = next(
            (index for index in range(end_index, -1, -1) if dated[index][0] <= start),
            None,
        )
        if start_index is None:
            return _range_result(
                status=STATUS_INSUFFICIENT,
                reason="历史不足，缺少区间起点估值",
                target_start=start,
                end=dated[end_index][0],
            )

    window = dated[start_index : end_index + 1]
    baseline_date = window[0][0]
    end_date = window[-1][0]
    if _nav_value(window[0][1]) is None:
        # Never skip the baseline and pretend a shorter window is complete.
        return _range_result(
            status=STATUS_INVALID,
            reason="区间起点净值缺失或无效",
            target_start=start,
            start=baseline_date,
            end=end_date,
        )

    peak: Decimal | None = None
    peak_day: date | None = None
    worst: Decimal | None = None
    worst_peak_day: date | None = None
    worst_trough_day: date | None = None
    worst_peak_value: Decimal | None = None
    worst_trough_value: Decimal | None = None
    observed = 0
    missing = 0
    points: list[RangeDrawdownPoint] = []

    for valuation_day, record in window:
        value = _nav_value(record)
        if value is None:
            missing += 1
            points.append(RangeDrawdownPoint(valuation_day, None, peak, None, worst))
            continue
        observed += 1
        if peak is None or value > peak:
            peak = value
            peak_day = valuation_day
        drawdown = value / peak - Decimal(1)
        if worst is None or drawdown < worst:
            worst = drawdown
            worst_peak_day = peak_day
            worst_trough_day = valuation_day
            worst_peak_value = peak
            worst_trough_value = value
        points.append(RangeDrawdownPoint(valuation_day, value, peak, drawdown, worst))

    current = points[-1].drawdown if points else None
    status = STATUS_OK if missing == 0 else STATUS_INCOMPLETE
    return _range_result(
        status=status,
        reason=None if missing == 0 else _MISSING_DATA_REASON,
        target_start=start,
        start=baseline_date,
        end=end_date,
        points=tuple(points),
        current_drawdown=current,
        max_drawdown=worst,
        peak_date=worst_peak_day,
        trough_date=worst_trough_day,
        peak_value=worst_peak_value,
        trough_value=worst_trough_value,
        observed_points=observed,
        missing_points=missing,
    )
