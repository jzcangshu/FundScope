"""Versioned valuation data and source provenance models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import (
    Base,
    ValidationLevel,
    ValuationStatus,
    created_at_column,
    enum_column,
)

if TYPE_CHECKING:
    from .catalog import Fund, ParserRuleSet
    from .imports import SourceFile


class ValuationVersion(Base):
    __tablename__ = "valuation_version"
    __table_args__ = (
        UniqueConstraint(
            "fund_id",
            "valuation_date",
            "version_no",
            name="uq_valuation_fund_date_version",
        ),
        Index(
            "uq_valuation_one_published_per_fund_date",
            "fund_id",
            "valuation_date",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
        Index("ix_valuation_fund_date_status", "fund_id", "valuation_date", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("fund.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    valuation_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL"), index=True
    )
    parser_rule_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("parser_rule_set.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = enum_column(
        ValuationStatus, nullable=False, default=ValuationStatus.RECEIVED
    )
    published_by: Mapped[str | None] = mapped_column(String(255))
    release_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    fund: Mapped[Fund] = relationship(back_populates="valuation_versions")
    source_file: Mapped[SourceFile | None] = relationship()
    parser_rule_set: Mapped[ParserRuleSet | None] = relationship()
    daily_snapshot: Mapped[FundDailySnapshot | None] = relationship(
        back_populates="valuation_version", uselist=False, passive_deletes=True
    )


class ValidationResult(Base):
    __tablename__ = "validation_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_version_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    level: Mapped[str] = enum_column(ValidationLevel, nullable=False)
    actual_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    source_location: Mapped[str | None] = mapped_column(String(500))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL")
    )
    ignored_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()


class FieldProvenance(Base):
    __tablename__ = "field_provenance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_version_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    standard_field: Mapped[str] = mapped_column(String(255), nullable=False)
    source_worksheet: Mapped[str | None] = mapped_column(String(255))
    source_row: Mapped[int | None] = mapped_column(Integer)
    source_column: Mapped[int | None] = mapped_column(Integer)
    raw_text: Mapped[str | None] = mapped_column(Text)
    transformation: Mapped[str | None] = mapped_column(String(255))


class FundDailySnapshot(Base):
    __tablename__ = "fund_daily_snapshot"
    __table_args__ = (
        UniqueConstraint("valuation_version_id", name="uq_fund_snapshot_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_version_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_version.id", ondelete="RESTRICT"), nullable=False
    )
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    net_asset_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    cumulative_unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    previous_unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    daily_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    ytd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    mtd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    qtd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    wtd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    cumulative_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    cumulative_payout: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    available_headroom: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))

    valuation_version: Mapped[ValuationVersion] = relationship(
        back_populates="daily_snapshot"
    )


class ShareClassDailySnapshot(Base):
    __tablename__ = "share_class_daily_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "valuation_version_id",
            "share_class_id",
            name="uq_share_snapshot_version_class",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_version_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    share_class_id: Mapped[int] = mapped_column(
        ForeignKey("share_class.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    net_assets: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    paid_in_capital: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    cumulative_unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    previous_unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    daily_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    ytd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    mtd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    qtd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    wtd_return: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))


class AccountSubjectDaily(Base):
    __tablename__ = "account_subject_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_version_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    raw_subject_code: Mapped[str | None] = mapped_column(String(100))
    raw_subject_name: Mapped[str] = mapped_column(String(255), nullable=False)
    standard_category: Mapped[str | None] = mapped_column(String(100))
    hierarchy_path: Mapped[str | None] = mapped_column(String(1000))
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    include_in_holdings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    cost_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    market_value_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    valuation_gain: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    suspension_info: Mapped[str | None] = mapped_column(Text)
    source_worksheet: Mapped[str | None] = mapped_column(String(255))
    source_row: Mapped[int | None] = mapped_column(Integer)


class PositionDaily(Base):
    __tablename__ = "position_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    valuation_version_id: Mapped[int] = mapped_column(
        ForeignKey("valuation_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    security_code: Mapped[str] = mapped_column(String(100), nullable=False)
    security_name: Mapped[str | None] = mapped_column(String(255))
    market: Mapped[str | None] = mapped_column(String(100))
    account: Mapped[str | None] = mapped_column(String(255))
    original_subject_code: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    nav_weight: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    valuation_gain: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    suspension_info: Mapped[str | None] = mapped_column(Text)
    source_worksheet: Mapped[str | None] = mapped_column(String(255))
    source_row: Mapped[int | None] = mapped_column(Integer)
