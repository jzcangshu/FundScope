"""Product master data and parsing configuration models."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import (
    Base,
    FundStatus,
    MappingStatus,
    ParserRuleStatus,
    created_at_column,
    enum_column,
    updated_at_column,
)

if TYPE_CHECKING:
    from .valuation import ValuationVersion


class Fund(Base):
    __tablename__ = "fund"
    __table_args__ = (
        Index(
            "uq_fund_product_code_not_null",
            "product_code",
            unique=True,
            postgresql_where=text("product_code IS NOT NULL"),
            sqlite_where=text("product_code IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    standard_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    product_code: Mapped[str | None] = mapped_column(String(100))
    establishment_date: Mapped[date | None] = mapped_column(Date)
    strategy: Mapped[str | None] = mapped_column(String(255))
    manager: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = enum_column(
        FundStatus, nullable=False, default=FundStatus.ACTIVE
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    aliases: Mapped[list[FundAlias]] = relationship(
        back_populates="fund", passive_deletes=True
    )
    share_classes: Mapped[list[ShareClass]] = relationship(
        back_populates="fund", passive_deletes=True
    )
    valuation_versions: Mapped[list[ValuationVersion]] = relationship(
        back_populates="fund", passive_deletes=True
    )


class FundAlias(Base):
    __tablename__ = "fund_alias"
    __table_args__ = (UniqueConstraint("fund_id", "alias", name="uq_fund_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("fund.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source_location: Mapped[str | None] = mapped_column(String(255))
    match_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = created_at_column()

    fund: Mapped[Fund] = relationship(back_populates="aliases")


class ShareClass(Base):
    __tablename__ = "share_class"
    __table_args__ = (
        UniqueConstraint("fund_id", "share_code", name="uq_share_class_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("fund.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    share_code: Mapped[str] = mapped_column(String(100), nullable=False)
    share_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled_from: Mapped[date | None] = mapped_column(Date)
    disabled_from: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    fund: Mapped[Fund] = relationship(back_populates="share_classes")


class SubjectMapping(Base):
    __tablename__ = "subject_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_code_or_prefix: Mapped[str | None] = mapped_column(String(100))
    raw_name_pattern: Mapped[str | None] = mapped_column(String(255))
    standard_category: Mapped[str] = mapped_column(String(100), nullable=False)
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_in_holdings: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = enum_column(
        MappingStatus, nullable=False, default=MappingStatus.ACTIVE
    )


class ParserRuleSet(Base):
    __tablename__ = "parser_rule_set"
    __table_args__ = (
        UniqueConstraint(
            "template_identifier", "version", name="uq_parser_rule_version"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(255), nullable=False)
    template_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = enum_column(
        ParserRuleStatus, nullable=False, default=ParserRuleStatus.DRAFT
    )
    created_at: Mapped[datetime] = created_at_column()
