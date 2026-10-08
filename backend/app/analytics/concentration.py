"""Position concentration calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ._common import decimal, field


@dataclass(frozen=True, slots=True)
class PositionWeight:
    security_code: str
    security_name: str | None
    market_value: Decimal
    weight: Decimal | None


@dataclass(frozen=True, slots=True)
class ConcentrationResult:
    positions: tuple[PositionWeight, ...]
    denominator: Decimal | None
    max_single_weight: Decimal | None
    top_five_weight: Decimal | None
    hhi: Decimal | None
    missing_value_count: int


def calculate_concentration(
    positions: list[Any] | tuple[Any, ...],
    *,
    net_asset_value: Decimal | float | str | None = None,
) -> ConcentrationResult:
    """Merge the same security across accounts and calculate concentration."""

    grouped: dict[str, tuple[str | None, Decimal]] = {}
    missing_value_count = 0
    for position in positions:
        raw_code = field(position, "security_code", "code")
        if raw_code is None or not str(raw_code).strip():
            raise ValueError("position is missing security_code")
        code = str(raw_code).strip()
        market_value = decimal(field(position, "market_value"))
        if market_value is None:
            missing_value_count += 1
            continue
        name = field(position, "security_name", "name")
        current_name, current_value = grouped.get(code, (None, Decimal(0)))
        grouped[code] = (
            current_name or (str(name).strip() if name else None),
            current_value + market_value,
        )

    total_market_value = sum((value for _, value in grouped.values()), Decimal(0))
    denominator = (
        decimal(net_asset_value) if net_asset_value is not None else total_market_value
    )
    result_positions = [
        PositionWeight(
            security_code=code,
            security_name=name,
            market_value=market_value,
            weight=None
            if denominator in (None, Decimal(0))
            else market_value / denominator,
        )
        for code, (name, market_value) in grouped.items()
    ]
    result_positions.sort(
        key=lambda item: (-abs(item.market_value), item.security_code)
    )
    absolute_weights = [
        abs(item.weight) for item in result_positions if item.weight is not None
    ]
    max_single_weight = max(absolute_weights, default=None)
    top_five_weight = (
        sum(absolute_weights[:5], Decimal(0)) if absolute_weights else None
    )
    hhi = sum((weight * weight for weight in absolute_weights), Decimal(0)) or (
        None if denominator in (None, Decimal(0)) else Decimal(0)
    )
    return ConcentrationResult(
        positions=tuple(result_positions),
        denominator=denominator,
        max_single_weight=max_single_weight,
        top_five_weight=top_five_weight,
        hhi=hhi,
        missing_value_count=missing_value_count,
    )


def calculate_position_concentration(
    positions: list[Any] | tuple[Any, ...],
    *,
    net_asset_value: Decimal | float | str | None = None,
) -> ConcentrationResult:
    return calculate_concentration(positions, net_asset_value=net_asset_value)
