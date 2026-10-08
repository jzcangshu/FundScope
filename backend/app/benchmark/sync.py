"""Benchmark index synchronization service.

Fetches daily index data from Tushare and upserts into BenchmarkDaily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import utcnow
from app.db.models.benchmark import BenchmarkDaily, BenchmarkSyncState

from .tushare_client import TushareClient, TushareError

logger = logging.getLogger(__name__)

# Tushare index_daily returns at most ~5000 rows per call.
# Use 4-year segments (~1000 trading days) to stay well under the limit.
_SEGMENT_YEARS = 4

# On incremental sync, re-fetch this many calendar days before the last known
# trade date to pick up Tushare's late-day revisions.
_INCREMENTAL_LOOKBACK_DAYS = 14

# Earliest date to fetch for CSI 300 (well before the index inception).
_DEFAULT_HISTORY_START = date(2005, 4, 8)

# Reasonable upper bound for sanity checks.
_MAX_CLOSE = Decimal(100000)
_MIN_CLOSE = Decimal("0.01")


@dataclass
class SyncResult:
    ts_code: str
    rows_upserted: int
    total_rows: int
    earliest: date | None
    latest: date | None
    error: str | None = None


def _parse_close(raw: Any) -> Decimal | None:
    """Parse a close price, returning None for unparseable values."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if value <= _MIN_CLOSE or value > _MAX_CLOSE:
        return None
    return value


def _parse_pct_chg(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(raw: Any) -> date | None:
    """Parse Tushare's YYYYMMDD string into a date."""
    if raw is None or not isinstance(raw, str) or len(raw) != 8:
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (ValueError, IndexError):
        return None


def _upsert_rows(
    session: Session,
    ts_code: str,
    rows: list[dict[str, Any]],
    source: str = "tushare",
) -> int:
    """Upsert parsed rows into BenchmarkDaily. Returns count of valid rows."""

    records: list[dict[str, Any]] = []
    for row in rows:
        trade_date = _parse_date(row.get("trade_date"))
        close = _parse_close(row.get("close"))
        if trade_date is None or close is None:
            continue
        records.append(
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "close": close,
                "pct_chg": _parse_pct_chg(row.get("pct_chg")),
                "source": source,
            }
        )

    if not records:
        return 0

    # Use PostgreSQL ON CONFLICT for idempotent upsert.  For SQLite, fall back
    # to a manual upsert pattern since its dialect doesn't support
    # INSERT ON CONFLICT DO UPDATE in the same way.
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        stmt = pg_insert(BenchmarkDaily).values(records)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_benchmark_code_date",
            set_={
                "close": stmt.excluded.close,
                "pct_chg": stmt.excluded.pct_chg,
                "updated_at": utcnow(),
            },
        )
        session.execute(stmt)
    else:
        # SQLite fallback: insert-or-ignore + manual update for existing rows.
        for record in records:
            existing = session.execute(
                select(BenchmarkDaily).where(
                    BenchmarkDaily.ts_code == record["ts_code"],
                    BenchmarkDaily.trade_date == record["trade_date"],
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(BenchmarkDaily(**record))
            else:
                existing.close = record["close"]
                existing.pct_chg = record["pct_chg"]
                existing.updated_at = utcnow()

    session.flush()
    return len(records)


def _get_sync_state(session: Session, ts_code: str) -> BenchmarkSyncState:
    """Load or create the singleton sync state row."""

    state = session.get(BenchmarkSyncState, 1)
    if state is None:
        state = BenchmarkSyncState(id=1, ts_code=ts_code, total_rows=0)
        session.add(state)
        session.flush()
    return state


def sync_benchmark(
    session: Session,
    settings: Settings,
    *,
    ts_code: str = "000300.SH",
    full: bool = False,
) -> SyncResult:
    """Fetch and persist benchmark index daily data from Tushare.

    When *full* is True (or no data exists yet), the entire history from
    ``_DEFAULT_HISTORY_START`` to today is fetched in segments.  Otherwise only
    a small incremental window around the last known trade date is re-fetched.

    Returns a SyncResult with counts and coverage, or an error message.
    """

    if not settings.tushare_token:
        return SyncResult(
            ts_code=ts_code,
            rows_upserted=0,
            total_rows=0,
            earliest=None,
            latest=None,
            error="TUSHARE_TOKEN not configured",
        )

    client = TushareClient(settings.tushare_token)
    state = _get_sync_state(session, ts_code)
    now = utcnow()

    state.last_attempt_at = now
    state.ts_code = ts_code

    try:
        last_trade_date = state.last_trade_date
        if full or last_trade_date is None:
            result = _full_sync(session, client, ts_code, state)
        else:
            result = _incremental_sync(session, client, ts_code, state, last_trade_date)

        state.last_success_at = utcnow()
        state.last_error = None
        session.commit()
        return result

    except (TushareError, OSError, TimeoutError) as exc:
        error_msg = str(exc)[:490]
        state.last_error = error_msg
        session.commit()
        logger.error("Benchmark sync failed: %s", error_msg)
        return SyncResult(
            ts_code=ts_code,
            rows_upserted=0,
            total_rows=state.total_rows,
            earliest=state.earliest_trade_date,
            latest=state.last_trade_date,
            error=error_msg,
        )


def _full_sync(
    session: Session,
    client: TushareClient,
    ts_code: str,
    state: BenchmarkSyncState,
) -> SyncResult:
    """Fetch full history in multi-year segments."""

    start = _DEFAULT_HISTORY_START
    end = datetime.now(UTC).date()
    total_upserted = 0

    while start <= end:
        segment_end = min(
            end,
            date(start.year + _SEGMENT_YEARS, start.month, start.day)
            - timedelta(days=1),
        )
        rows = client.index_daily(ts_code, start, segment_end)
        if not rows:
            # No more data in this segment; move to the next.
            start = segment_end + timedelta(days=1)
            continue
        count = _upsert_rows(session, ts_code, rows)
        total_upserted += count
        logger.info("Benchmark sync segment %s..%s: %d rows", start, segment_end, count)
        # If we got fewer rows than expected, we've likely reached the end.
        if len(rows) < 100:
            break
        start = segment_end + timedelta(days=1)

    _refresh_state(session, state)
    return SyncResult(
        ts_code=ts_code,
        rows_upserted=total_upserted,
        total_rows=state.total_rows,
        earliest=state.earliest_trade_date,
        latest=state.last_trade_date,
    )


def _incremental_sync(
    session: Session,
    client: TushareClient,
    ts_code: str,
    state: BenchmarkSyncState,
    last_trade_date: date,
) -> SyncResult:
    """Re-fetch a small window around the last known trade date."""

    end = datetime.now(UTC).date()
    start = last_trade_date - timedelta(days=_INCREMENTAL_LOOKBACK_DAYS)

    rows = client.index_daily(ts_code, start, end)
    count = _upsert_rows(session, ts_code, rows)

    _refresh_state(session, state)
    return SyncResult(
        ts_code=ts_code,
        rows_upserted=count,
        total_rows=state.total_rows,
        earliest=state.earliest_trade_date,
        latest=state.last_trade_date,
    )


def _refresh_state(session: Session, state: BenchmarkSyncState) -> None:
    """Recompute sync state aggregates from the actual table contents."""

    result = session.execute(
        select(
            func.count().label("total"),
            func.min(BenchmarkDaily.trade_date).label("earliest"),
            func.max(BenchmarkDaily.trade_date).label("latest"),
        ).where(BenchmarkDaily.ts_code == state.ts_code)
    ).one()
    state.total_rows = result.total or 0
    state.earliest_trade_date = result.earliest
    state.last_trade_date = result.latest


__all__ = ["SyncResult", "sync_benchmark"]
