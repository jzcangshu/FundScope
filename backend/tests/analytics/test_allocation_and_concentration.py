from decimal import Decimal

from app.analytics.allocation import calculate_asset_allocation
from app.analytics.concentration import calculate_concentration


def test_asset_allocation_aggregates_categories_and_uses_nav_denominator() -> None:
    result = calculate_asset_allocation(
        [
            {"standard_category": "股票", "market_value": Decimal(30)},
            {"standard_category": "股票", "market_value": Decimal(20)},
            {"standard_category": "现金", "market_value": Decimal(50)},
        ],
        net_asset_value=Decimal(100),
    )

    assert {item.category: item.weight for item in result.items} == {
        "股票": Decimal("0.5"),
        "现金": Decimal("0.5"),
    }


def test_position_concentration_merges_accounts_and_calculates_top_five_and_hhi() -> (
    None
):
    result = calculate_concentration(
        [
            {"security_code": "A", "security_name": "甲", "market_value": Decimal(20)},
            {"security_code": "A", "security_name": "甲", "market_value": Decimal(10)},
            {"security_code": "B", "security_name": "乙", "market_value": Decimal(10)},
        ],
        net_asset_value=Decimal(100),
    )

    assert result.positions[0].market_value == Decimal(30)
    assert result.max_single_weight == Decimal("0.3")
    assert result.top_five_weight == Decimal("0.4")
    assert result.hhi == Decimal("0.1")


def test_weights_are_none_when_denominator_is_zero() -> None:
    allocation = calculate_asset_allocation(
        [{"category": "股票", "market_value": Decimal(10)}],
        net_asset_value=Decimal(0),
    )
    concentration = calculate_concentration(
        [{"security_code": "A", "market_value": Decimal(10)}],
        net_asset_value=Decimal(0),
    )

    assert allocation.items[0].weight is None
    assert concentration.max_single_weight is None
    assert concentration.top_five_weight is None
    assert concentration.hhi is None


def test_asset_allocation_excludes_non_leaf_and_non_holding_subjects() -> None:
    result = calculate_asset_allocation(
        [
            {
                "category": "股票",
                "market_value": Decimal(100),
                "is_leaf": False,
                "include_in_holdings": False,
            },
            {
                "category": "现金",
                "market_value": Decimal(40),
                "is_leaf": True,
                "include_in_holdings": True,
            },
        ],
        total_assets=Decimal(100),
    )

    assert [(item.category, item.market_value) for item in result.items] == [
        ("现金", Decimal(40))
    ]
    assert result.items[0].weight == Decimal("0.4")
