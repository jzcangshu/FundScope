"""Benchmark index daily data and sync state models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, created_at_column, updated_at_column


class BenchmarkDaily(Base):
    __tablename__ = "benchmark_daily"
    __table_args__ = (
        UniqueConstraint("ts_code", "trade_date", name="uq_benchmark_code_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    pct_chg: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="tushare")
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()


class BenchmarkSyncState(Base):
    """Singleton row tracking benchmark sync progress (id always = 1)."""

    __tablename__ = "benchmark_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    ts_code: Mapped[str] = mapped_column(String(20), nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_trade_date: Mapped[date | None] = mapped_column(Date)
    earliest_trade_date: Mapped[date | None] = mapped_column(Date)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = updated_at_column()
