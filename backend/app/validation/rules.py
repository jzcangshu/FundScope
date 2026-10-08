"""Pure validation rules for normalized valuation data.

The functions in this module deliberately do not know about SQLAlchemy.  They
accept scalar values or the small immutable parser records and return stable
findings that can be rendered by an API, persisted by the service layer, or
tested without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.db.base import ValidationLevel, ValuationStatus
from app.parser.interface import ParsedValuation

ASSET_LIABILITY_BALANCE = "asset_liability_balance"
SHARE_NET_ASSET_TOTAL = "share_net_asset_total"
DAILY_RETURN_RECONCILIATION = "daily_return_reconciliation"
POSITION_MARKET_VALUE_RECONCILIATION = "position_market_value_reconciliation"
IDENTITY_PRODUCT = "valuation_product_identity"
IDENTITY_DATE = "valuation_date_identity"
PARSER_WARNING = "parser_warning"
DEFAULT_MONEY_TOLERANCE = Decimal("0.01")
DEFAULT_NAV_TOLERANCE = Decimal("0.0001")
DEFAULT_RATIO_TOLERANCE = Decimal("0.000001")
# Position rules need both an absolute floor (small positions) and a relative
# ceiling (large positions where 0.01 is too tight). Defaults match the old
# absolute behaviour for tiny positions and allow 0.01% drift on bigger ones.
DEFAULT_POSITION_RELATIVE_TOLERANCE = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ToleranceConfig:
    """Comparison tolerances in the units used by normalized data."""

    money: Decimal = DEFAULT_MONEY_TOLERANCE
    nav: Decimal = DEFAULT_NAV_TOLERANCE
    ratio: Decimal = DEFAULT_RATIO_TOLERANCE
    position_relative: Decimal = DEFAULT_POSITION_RELATIVE_TOLERANCE


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One deterministic validation result suitable for persistence."""

    rule_code: str
    level: ValidationLevel
    actual_value: Decimal | None = None
    expected_value: Decimal | None = None
    difference: Decimal | None = None
    source_location: str | None = None
    message: str = ""

    @property
    def blocks_publish(self) -> bool:
        return self.level == ValidationLevel.CRITICAL


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The complete result of running the configured validation rules."""

    findings: tuple[ValidationFinding, ...]
    status: ValuationStatus

    @property
    def critical_count(self) -> int:
        return sum(item.level == ValidationLevel.CRITICAL for item in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(item.level == ValidationLevel.WARNING for item in self.findings)

    @property
    def info_count(self) -> int:
        return sum(item.level == ValidationLevel.INFO for item in self.findings)

    @property
    def publishable(self) -> bool:
        return self.status == ValuationStatus.PUBLISHABLE


def check_asset_liability_balance(
    total_assets: Any,
    total_liabilities: Any,
    net_asset_value: Any,
    *,
    tolerance: Decimal = DEFAULT_MONEY_TOLERANCE,
) -> ValidationFinding:
    """Check ``assets - liabilities = net assets`` within a money tolerance."""

    assets = _to_decimal(total_assets)
    liabilities = _to_decimal(total_liabilities)
    net_assets = _to_decimal(net_asset_value)
    if assets is None and liabilities is None:
        # NAV-only sources (broker NAV downloads) carry no balance sheet at
        # all; there is nothing to reconcile, which is not a failed check.
        return ValidationFinding(
            rule_code=ASSET_LIABILITY_BALANCE,
            level=ValidationLevel.INFO,
            source_location="summary.total_assets,total_liabilities,net_asset_value",
            message="资产与负债字段均缺失，暂不执行资产负债平衡校验",
        )
    if assets is None or liabilities is None or net_assets is None:
        return ValidationFinding(
            rule_code=ASSET_LIABILITY_BALANCE,
            level=ValidationLevel.CRITICAL,
            source_location="summary.total_assets,total_liabilities,net_asset_value",
            message="资产、负债或基金资产净值缺失，无法完成资产负债平衡校验",
        )

    calculated = assets - liabilities
    difference = calculated - net_assets
    if abs(difference) > tolerance:
        return ValidationFinding(
            rule_code=ASSET_LIABILITY_BALANCE,
            level=ValidationLevel.CRITICAL,
            actual_value=calculated,
            expected_value=net_assets,
            difference=difference,
            source_location="summary.total_assets,total_liabilities,net_asset_value",
            message="资产总额减负债总额与基金资产净值不一致",
        )
    return ValidationFinding(
        rule_code=ASSET_LIABILITY_BALANCE,
        level=ValidationLevel.INFO,
        actual_value=calculated,
        expected_value=net_assets,
        difference=difference,
        source_location="summary.total_assets,total_liabilities,net_asset_value",
        message="资产负债平衡校验通过",
    )


def check_share_net_assets_total(
    share_classes: Iterable[Any],
    net_asset_value: Any,
    *,
    tolerance: Decimal = DEFAULT_MONEY_TOLERANCE,
) -> ValidationFinding:
    """Check the sum of share-class net assets against fund net assets."""

    classes = tuple(share_classes)
    net_assets = _to_decimal(net_asset_value)
    if not classes:
        return ValidationFinding(
            rule_code=SHARE_NET_ASSET_TOTAL,
            level=ValidationLevel.INFO,
            source_location="share_classes",
            message="未发现份额类别数据，暂不执行份额净资产合计校验",
        )
    if net_assets is None:
        return ValidationFinding(
            rule_code=SHARE_NET_ASSET_TOTAL,
            level=ValidationLevel.CRITICAL,
            source_location="summary.net_asset_value",
            message="基金资产净值缺失，无法完成份额净资产合计校验",
        )

    values = [_to_decimal(getattr(item, "net_assets", None)) for item in classes]
    if any(value is None for value in values):
        return ValidationFinding(
            rule_code=SHARE_NET_ASSET_TOTAL,
            level=ValidationLevel.CRITICAL,
            expected_value=net_assets,
            source_location="share_classes.net_assets",
            message="存在份额类别净资产缺失，无法完成份额净资产合计校验",
        )

    calculated_values = [value for value in values if value is not None]
    calculated = sum(calculated_values, Decimal(0))
    difference = calculated - net_assets
    if abs(difference) > tolerance:
        return ValidationFinding(
            rule_code=SHARE_NET_ASSET_TOTAL,
            level=ValidationLevel.CRITICAL,
            actual_value=calculated,
            expected_value=net_assets,
            difference=difference,
            source_location="share_classes.net_assets,summary.net_asset_value",
            message="份额类别净资产合计与基金资产净值不一致",
        )
    return ValidationFinding(
        rule_code=SHARE_NET_ASSET_TOTAL,
        level=ValidationLevel.INFO,
        actual_value=calculated,
        expected_value=net_assets,
        difference=difference,
        source_location="share_classes.net_assets,summary.net_asset_value",
        message="份额净资产合计校验通过",
    )


def check_daily_return(
    unit_nav: Any,
    previous_unit_nav: Any,
    reported_daily_return: Any,
    *,
    tolerance: Decimal = DEFAULT_RATIO_TOLERANCE,
) -> ValidationFinding:
    """Compare reported daily return with the current/previous unit NAV ratio."""

    current = _to_decimal(unit_nav)
    previous = _to_decimal(previous_unit_nav)
    reported = _to_decimal(reported_daily_return)
    if current is None or previous is None or reported is None:
        return ValidationFinding(
            rule_code=DAILY_RETURN_RECONCILIATION,
            level=ValidationLevel.INFO,
            source_location="summary.unit_nav,previous_unit_nav,daily_return",
            message="净值日收益所需字段不完整，暂不执行计算校验",
        )
    if previous == 0:
        return ValidationFinding(
            rule_code=DAILY_RETURN_RECONCILIATION,
            level=ValidationLevel.WARNING,
            actual_value=reported,
            source_location="summary.previous_unit_nav",
            message="昨日单位净值为零，无法计算净值日收益",
        )

    calculated = current / previous - Decimal(1)
    difference = calculated - reported
    if abs(difference) > tolerance:
        return ValidationFinding(
            rule_code=DAILY_RETURN_RECONCILIATION,
            level=ValidationLevel.WARNING,
            actual_value=calculated,
            expected_value=reported,
            difference=difference,
            source_location="summary.unit_nav,previous_unit_nav,daily_return",
            message="净值日收益与今昨单位净值计算结果存在差异",
        )
    return ValidationFinding(
        rule_code=DAILY_RETURN_RECONCILIATION,
        level=ValidationLevel.INFO,
        actual_value=calculated,
        expected_value=reported,
        difference=difference,
        source_location="summary.unit_nav,previous_unit_nav,daily_return",
        message="净值日收益校验通过",
    )


def check_position_market_value(
    position: Any,
    *,
    position_label: str | None = None,
    tolerance: Decimal = DEFAULT_MONEY_TOLERANCE,
    relative_tolerance: Decimal = DEFAULT_POSITION_RELATIVE_TOLERANCE,
) -> ValidationFinding:
    """Check one position's quantity multiplied by price against market value.

    A flat 0.01 yuan tolerance both misses large discrepancies on million-RMB
    positions and false-positives on tiny ones, so we compare against
    max(absolute_tolerance, market_value * relative_tolerance).
    """

    quantity = _to_decimal(getattr(position, "quantity", None))
    market_price = _to_decimal(getattr(position, "market_price", None))
    market_value = _to_decimal(getattr(position, "market_value", None))
    label = position_label or getattr(position, "security_code", None) or "unknown"
    source = f"positions[{label}].quantity,market_price,market_value"
    if quantity is None or market_price is None or market_value is None:
        return ValidationFinding(
            rule_code=POSITION_MARKET_VALUE_RECONCILIATION,
            level=ValidationLevel.INFO,
            source_location=source,
            message="持仓数量、市价或市值不完整，暂不执行数量乘市价校验",
        )

    calculated = quantity * market_price
    difference = calculated - market_value
    effective_tolerance = max(tolerance, abs(market_value) * relative_tolerance)
    if abs(difference) > effective_tolerance:
        return ValidationFinding(
            rule_code=POSITION_MARKET_VALUE_RECONCILIATION,
            level=ValidationLevel.WARNING,
            actual_value=calculated,
            expected_value=market_value,
            difference=difference,
            source_location=source,
            message=f"持仓 {label} 的数量乘市价与市值存在差异",
        )
    return ValidationFinding(
        rule_code=POSITION_MARKET_VALUE_RECONCILIATION,
        level=ValidationLevel.INFO,
        actual_value=calculated,
        expected_value=market_value,
        difference=difference,
        source_location=source,
        message=f"持仓 {label} 的数量乘市价校验通过",
    )


def validate_parsed_valuation(
    parsed: ParsedValuation,
    *,
    tolerances: ToleranceConfig | None = None,
) -> ValidationReport:
    """Run all first-release rules against a parser result."""

    config = tolerances or ToleranceConfig()
    findings: list[ValidationFinding] = []
    if parsed.product_name is None:
        findings.append(
            ValidationFinding(
                rule_code=IDENTITY_PRODUCT,
                level=ValidationLevel.CRITICAL,
                source_location="workbook.identity.product",
                message="无法识别估值表产品",
            )
        )
    else:
        findings.append(
            ValidationFinding(
                rule_code=IDENTITY_PRODUCT,
                level=ValidationLevel.INFO,
                source_location="workbook.identity.product",
                message="估值表产品识别通过",
            )
        )
    if parsed.valuation_date is None:
        findings.append(
            ValidationFinding(
                rule_code=IDENTITY_DATE,
                level=ValidationLevel.CRITICAL,
                source_location="workbook.identity.valuation_date",
                message="无法识别估值日期",
            )
        )
    else:
        findings.append(
            ValidationFinding(
                rule_code=IDENTITY_DATE,
                level=ValidationLevel.INFO,
                source_location="workbook.identity.valuation_date",
                message="估值日期识别通过",
            )
        )

    findings.extend(
        _validate_values(
            total_assets=parsed.total_assets,
            total_liabilities=parsed.total_liabilities,
            net_asset_value=parsed.net_asset_value,
            unit_nav=parsed.unit_nav,
            previous_unit_nav=parsed.previous_unit_nav,
            daily_return=parsed.daily_return,
            share_classes=parsed.share_classes,
            positions=parsed.positions,
            tolerances=config,
        )
    )
    findings.extend(
        ValidationFinding(
            rule_code=PARSER_WARNING,
            level=ValidationLevel.INFO,
            source_location="workbook.parser",
            message=f"解析提示：{warning}",
        )
        for warning in parsed.warnings
    )
    return _report(findings)


def validate_values(
    *,
    total_assets: Any,
    total_liabilities: Any,
    net_asset_value: Any,
    unit_nav: Any,
    previous_unit_nav: Any,
    daily_return: Any,
    share_classes: Iterable[Any] = (),
    positions: Iterable[Any] = (),
    tolerances: ToleranceConfig | None = None,
) -> ValidationReport:
    """Run the numeric rules against ORM rows or compatible value objects."""

    return _report(
        _validate_values(
            total_assets=total_assets,
            total_liabilities=total_liabilities,
            net_asset_value=net_asset_value,
            unit_nav=unit_nav,
            previous_unit_nav=previous_unit_nav,
            daily_return=daily_return,
            share_classes=share_classes,
            positions=positions,
            tolerances=tolerances or ToleranceConfig(),
        )
    )


def _validate_values(
    *,
    total_assets: Any,
    total_liabilities: Any,
    net_asset_value: Any,
    unit_nav: Any,
    previous_unit_nav: Any,
    daily_return: Any,
    share_classes: Iterable[Any],
    positions: Iterable[Any],
    tolerances: ToleranceConfig,
) -> list[ValidationFinding]:
    findings = [
        check_asset_liability_balance(
            total_assets,
            total_liabilities,
            net_asset_value,
            tolerance=tolerances.money,
        ),
        check_share_net_assets_total(
            share_classes,
            net_asset_value,
            tolerance=tolerances.money,
        ),
        check_daily_return(
            unit_nav,
            previous_unit_nav,
            daily_return,
            tolerance=tolerances.ratio,
        ),
    ]
    findings.extend(
        check_position_market_value(
            position,
            position_label=getattr(position, "security_code", None) or str(index),
            tolerance=tolerances.money,
            relative_tolerance=tolerances.position_relative,
        )
        for index, position in enumerate(positions, start=1)
    )
    if not any(
        item.rule_code == POSITION_MARKET_VALUE_RECONCILIATION for item in findings
    ):
        findings.append(
            ValidationFinding(
                rule_code=POSITION_MARKET_VALUE_RECONCILIATION,
                level=ValidationLevel.INFO,
                source_location="positions",
                message="未发现持仓明细，暂不执行数量乘市价校验",
            )
        )
    return findings


def _report(findings: Sequence[ValidationFinding]) -> ValidationReport:
    frozen = tuple(findings)
    status = (
        ValuationStatus.PENDING_REVIEW
        if any(item.blocks_publish for item in frozen)
        else ValuationStatus.PUBLISHABLE
    )
    return ValidationReport(findings=frozen, status=status)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


__all__ = [
    "ASSET_LIABILITY_BALANCE",
    "DAILY_RETURN_RECONCILIATION",
    "IDENTITY_DATE",
    "IDENTITY_PRODUCT",
    "PARSER_WARNING",
    "POSITION_MARKET_VALUE_RECONCILIATION",
    "SHARE_NET_ASSET_TOTAL",
    "ToleranceConfig",
    "ValidationFinding",
    "ValidationReport",
    "check_asset_liability_balance",
    "check_daily_return",
    "check_position_market_value",
    "check_share_net_assets_total",
    "validate_parsed_valuation",
    "validate_values",
]
