"""Small, dependency-free interface returned by Excel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Provenance:
    """Trace one normalized value back to its workbook cell."""

    standard_field: str
    worksheet: str
    row: int
    column: int
    raw_text: str
    transformation: str


@dataclass(frozen=True, slots=True)
class ParsedSubject:
    code: str
    name: str
    quantity: Decimal | None
    cost: Decimal | None
    cost_weight: Decimal | None
    market_value: Decimal | None
    market_value_weight: Decimal | None
    valuation_gain: Decimal | None
    suspension_info: str | None
    is_leaf: bool
    hierarchy_path: tuple[str, ...]
    source_row: int
    unit_cost: Decimal | None = None
    market_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ParsedPosition:
    security_code: str
    security_name: str
    quantity: Decimal | None
    unit_cost: Decimal | None
    cost: Decimal | None
    market_price: Decimal | None
    market_value: Decimal | None
    nav_weight: Decimal | None
    valuation_gain: Decimal | None
    suspension_info: str | None
    source_subject_code: str
    market: str | None
    account: str | None
    source_row: int


@dataclass(frozen=True, slots=True)
class ParsedShareClass:
    share_code: str
    share_name: str
    net_assets: Decimal | None
    paid_in_capital: Decimal | None
    unit_nav: Decimal | None
    cumulative_unit_nav: Decimal | None
    previous_unit_nav: Decimal | None
    daily_return: Decimal | None


@dataclass(frozen=True, slots=True)
class ParsedValuation:
    product_name: str | None
    product_candidates: tuple[str, ...]
    valuation_date: date | None
    worksheet: str
    total_assets: Decimal | None
    total_liabilities: Decimal | None
    net_asset_value: Decimal | None
    unit_nav: Decimal | None
    cumulative_unit_nav: Decimal | None
    previous_unit_nav: Decimal | None
    daily_return: Decimal | None
    ytd_return: Decimal | None
    mtd_return: Decimal | None
    qtd_return: Decimal | None
    wtd_return: Decimal | None
    cumulative_return: Decimal | None
    cumulative_payout: Decimal | None
    available_headroom: Decimal | None
    subjects: tuple[ParsedSubject, ...] = field(default_factory=tuple)
    positions: tuple[ParsedPosition, ...] = field(default_factory=tuple)
    share_classes: tuple[ParsedShareClass, ...] = field(default_factory=tuple)
    provenance: tuple[Provenance, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    template_identifier: str = "valuation-table-v1"
