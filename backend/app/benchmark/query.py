"""Benchmark data query helpers.

Provides batch queries and date-alignment logic for benchmark comparison.
All functions read only from the local database.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.benchmark import BenchmarkDaily, BenchmarkSyncState


def benchmark_series_for_range(
    session: Session,
    start: date,
    end: date,
    ts_code: str = "000300.SH",
) -> list[BenchmarkDaily]:
    """Return benchmark daily rows sorted by trade_date ascending."""

    rows = list(
        session.execute(
            select(BenchmarkDaily)
            .where(
                BenchmarkDaily.ts_code == ts_code,
                BenchmarkDaily.trade_date >= start,
                BenchmarkDaily.trade_date <= end,
            )
            .order_by(BenchmarkDaily.trade_date)
        ).scalars()
    )
    return rows


def benchmark_state(
    session: Session, ts_code: str = "000300.SH"
) -> BenchmarkSyncState | None:
    """Return the sync state singleton, or None if never synced."""

    return session.get(BenchmarkSyncState, 1)


def align_benchmark_to_dates(
    benchmark_rows: list[BenchmarkDaily],
    target_dates: list[date],
) -> dict[date, BenchmarkDaily | None]:
    """Map each target date to the benchmark row on or before it.

    For non-trading days, the closest *prior* trading day's row is used.
    If the target date is before the earliest available benchmark row, the
    mapping value is ``None`` (never use future data to fill a gap).
    """

    if not benchmark_rows or not target_dates:
        return {d: None for d in target_dates}

    trade_dates = [r.trade_date for r in benchmark_rows]
    by_date = {r.trade_date: r for r in benchmark_rows}

    result: dict[date, BenchmarkDaily | None] = {}
    for target in target_dates:
        if target in by_date:
            result[target] = by_date[target]
        else:
            idx = bisect_right(trade_dates, target) - 1
            if idx >= 0:
                result[target] = by_date[trade_dates[idx]]
            else:
                result[target] = None
    return result


def benchmark_return(
    aligned: dict[date, BenchmarkDaily | None],
    start_date: date,
    end_date: date,
) -> tuple[Decimal | None, str | None]:
    """Compute benchmark return between two dates.

    Returns (return_ratio, unavailable_reason).  The return ratio uses the
    close-to-close formula: end_close / start_close - 1.
    """

    start_row = aligned.get(start_date)
    end_row = aligned.get(end_date)

    if start_row is None:
        return None, "benchmark_missing_start"
    if end_row is None:
        return None, "benchmark_missing_end"
    if start_row.close is None or start_row.close == 0:
        return None, "benchmark_invalid_start_price"
    if end_row.close is None or end_row.close == 0:
        return None, "benchmark_invalid_end_price"

    return end_row.close / start_row.close - Decimal(1), None


__all__ = [
    "align_benchmark_to_dates",
    "benchmark_return",
    "benchmark_series_for_range",
    "benchmark_state",
]
