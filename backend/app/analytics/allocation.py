"""Asset allocation weights."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ._common import decimal, field


@dataclass(frozen=True, slots=True)
class AllocationItem:
    category: str
    market_value: Decimal
    weight: Decimal | None


@dataclass(frozen=True, slots=True)
class AllocationResult:
    items: tuple[AllocationItem, ...]
    denominator: Decimal | None
    total_market_value: Decimal
    missing_value_count: int


def calculate_asset_allocation(
    holdings: list[Any] | tuple[Any, ...],
    *,
    net_asset_value: Decimal | float | str | None = None,
    total_assets: Decimal | float | str | None = None,
    denominator_type: str | None = None,
) -> AllocationResult:
    """Aggregate eligible leaf holdings by category and calculate weights.

    ``denominator_type`` accepts ``net_asset_value``, ``total_assets`` or
    ``market_value``.  Without it, the first supplied balance-sheet value is
    used; when neither is supplied, the included market values are the
    denominator.
    """

    grouped: dict[str, Decimal] = {}
    missing_value_count = 0
    for holding in holdings:
        include_in_holdings = field(holding, "include_in_holdings")
        is_leaf = field(holding, "is_leaf")
        if include_in_holdings is not None and not bool(include_in_holdings):
            continue
        if is_leaf is not None and not bool(is_leaf):
            continue
        category = field(holding, "standard_category", "category")
        if category is None or not str(category).strip():
            raise ValueError("holding is missing standard_category")
        market_value = decimal(field(holding, "market_value"))
        if market_value is None:
            missing_value_count += 1
            continue
        category_name = str(category).strip()
        grouped[category_name] = grouped.get(category_name, Decimal(0)) + market_value

    total_market_value = sum(grouped.values(), Decimal(0))
    if denominator_type is not None and denominator_type not in {
        "net_asset_value",
        "total_assets",
        "market_value",
    }:
        raise ValueError(f"unsupported denominator_type: {denominator_type}")
    selected_type = denominator_type
    if selected_type is None:
        selected_type = (
            "net_asset_value"
            if net_asset_value is not None
            else "total_assets"
            if total_assets is not None
            else "market_value"
        )
    denominator_source = {
        "net_asset_value": net_asset_value,
        "total_assets": total_assets,
        "market_value": total_market_value,
    }[selected_type]
    denominator = decimal(denominator_source)
    items = [
        AllocationItem(
            category=category,
            market_value=market_value,
            weight=None
            if denominator in (None, Decimal(0))
            else market_value / denominator,
        )
        for category, market_value in grouped.items()
    ]
    items.sort(key=lambda item: (-abs(item.market_value), item.category))
    return AllocationResult(
        items=tuple(items),
        denominator=denominator,
        total_market_value=total_market_value,
        missing_value_count=missing_value_count,
    )
