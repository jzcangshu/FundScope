"""Analysis output and risk metadata models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import (
    AnalysisRunStatus,
    Base,
    RiskEventStatus,
    RiskSeverity,
    created_at_column,
    enum_column,
)


class AnalysisRun(Base):
    __tablename__ = "analysis_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger_version_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "valuation_version.id",
            name="fk_analysis_run_trigger_version_id",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    trigger_reason: Mapped[str] = mapped_column(String(255), nullable=False)
    input_start_date: Mapped[date | None] = mapped_column(Date)
    input_end_date: Mapped[date | None] = mapped_column(Date)
    input_version_range: Mapped[str | None] = mapped_column(String(500))
    methodology_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = enum_column(AnalysisRunStatus, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()


class FundMetricDaily(Base):
    __tablename__ = "fund_metric_daily"
    __table_args__ = (
        UniqueConstraint(
            "fund_id",
            "valuation_date",
            "source_analysis_run_id",
            name="uq_fund_metric_run_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("fund.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source_analysis_run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    daily_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    cumulative_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    drawdown: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    historical_peak: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    concentration: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    asset_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))


class CompanyMetricDaily(Base):
    __tablename__ = "company_metric_daily"
    __table_args__ = (
        UniqueConstraint(
            "valuation_date",
            "source_analysis_run_id",
            name="uq_company_metric_run_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    source_analysis_run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    company_index: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    company_daily_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    effective_fund_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    total_net_assets: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))


class RiskRule(Base):
    __tablename__ = "risk_rule"
    __table_args__ = (
        UniqueConstraint("rule_code", "version", name="uq_risk_rule_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    severity: Mapped[str] = enum_column(RiskSeverity, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RiskEvent(Base):
    __tablename__ = "risk_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    risk_rule_id: Mapped[int] = mapped_column(
        ForeignKey("risk_rule.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund.id", ondelete="RESTRICT"), index=True
    )
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    severity: Mapped[str] = enum_column(RiskSeverity, nullable=False)
    status: Mapped[str] = enum_column(
        RiskEventStatus, nullable=False, default=RiskEventStatus.OPEN
    )
    first_triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    handling_note: Mapped[str | None] = mapped_column(Text)
    evidence_snapshot: Mapped[str | None] = mapped_column(Text)
    handled_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL"), index=True
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_reference: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = created_at_column()
