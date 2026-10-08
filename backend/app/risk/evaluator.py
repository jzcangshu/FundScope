"""Evaluate configured risk rules without database or API dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.analytics._common import decimal, field, valuation_date


@dataclass(frozen=True, slots=True)
class RiskRule:
    rule_code: str
    rule_type: str
    threshold: Decimal
    severity: str = "warning"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RiskEvent:
    rule_code: str
    rule_type: str
    severity: str
    valuation_date: date
    fund_id: Any | None
    observed_value: Decimal
    threshold: Decimal
    message: str


_DOWNSIDE_RULES = {"daily_return", "max_drawdown", "current_drawdown"}
_WEIGHT_RULES = {"single_position_weight", "top_five_weight", "concentration"}
_METRIC_ALIASES = {
    "single_position_weight": ("single_position_weight", "max_single_weight"),
    "top_five_weight": ("top_five_weight",),
    "concentration": ("concentration", "hhi"),
}


def evaluate_risk_rules(
    metrics: Any,
    rules: list[Any] | tuple[Any, ...],
    *,
    valuation_date_value: date | str,
    fund_id: Any | None = None,
) -> tuple[RiskEvent, ...]:
    """Return triggered events; missing metrics and disabled rules are ignored."""

    day = valuation_date(valuation_date_value)
    events: list[RiskEvent] = []
    seen_codes: set[str] = set()
    for rule in rules:
        code = str(field(rule, "rule_code", "code") or "").strip()
        if not code:
            raise ValueError("risk rule is missing rule_code")
        if code in seen_codes:
            raise ValueError(f"duplicate risk rule code: {code}")
        seen_codes.add(code)
        if not bool(field(rule, "enabled", default=True)):
            continue
        rule_type = str(field(rule, "rule_type", "type") or "").strip()
        threshold = decimal(field(rule, "threshold"))
        if not rule_type or threshold is None:
            raise ValueError(f"risk rule {code} is incomplete")
        metric_names = _METRIC_ALIASES.get(rule_type, (rule_type,))
        observed = decimal(field(metrics, *metric_names))
        if observed is None:
            continue
        if rule_type in _DOWNSIDE_RULES:
            trigger = observed <= (threshold if threshold < 0 else -threshold)
        elif rule_type in _WEIGHT_RULES:
            trigger = abs(observed) >= abs(threshold)
        else:
            raise ValueError(f"unsupported risk rule type: {rule_type}")
        if trigger:
            severity = str(field(rule, "severity", default="warning"))
            events.append(
                RiskEvent(
                    rule_code=code,
                    rule_type=rule_type,
                    severity=severity,
                    valuation_date=day,
                    fund_id=fund_id,
                    observed_value=observed,
                    threshold=threshold,
                    message=f"{rule_type} reached {observed}, threshold {threshold}",
                )
            )
    return tuple(events)
