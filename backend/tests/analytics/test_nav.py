from datetime import date
from decimal import Decimal

import pytest

from app.analytics.nav import calculate_nav_series


def test_nav_series_sorts_dates_and_calculates_cumulative_return() -> None:
    result = calculate_nav_series(
        [
            {
                "valuation_date": date(2026, 1, 2),
                "cumulative_unit_nav": Decimal("1.10"),
            },
            {
                "valuation_date": date(2026, 1, 1),
                "cumulative_unit_nav": Decimal("1.00"),
            },
        ]
    )

    assert [point.valuation_date for point in result.points] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
    ]
    assert result.points[1].daily_return == Decimal("0.1")
    assert result.total_return == Decimal("0.1")
    assert result.methodology == "cumulative_unit_nav"


def test_nav_series_adjusts_unit_nav_with_cumulative_payout() -> None:
    result = calculate_nav_series(
        [
            {
                "date": date(2026, 1, 1),
                "unit_nav": Decimal("1.00"),
                "cumulative_payout": Decimal(0),
            },
            {
                "date": date(2026, 1, 2),
                "unit_nav": Decimal("1.10"),
                "cumulative_payout": Decimal("0.10"),
            },
        ]
    )

    assert result.methodology == "unit_nav_plus_cumulative_payout"
    assert result.points[1].adjusted_nav == Decimal("1.20")
    assert result.total_return == Decimal("0.2")


def test_nav_series_keeps_missing_values_and_zero_denominator_explicit() -> None:
    result = calculate_nav_series(
        [
            {"date": date(2026, 1, 1), "unit_nav": Decimal(0)},
            {"date": date(2026, 1, 2), "unit_nav": Decimal(1)},
        ]
    )

    assert result.points[1].daily_return is None
    assert result.points[1].cumulative_return is None


def test_nav_series_rejects_duplicate_dates() -> None:
    with pytest.raises(ValueError, match="duplicate valuation date"):
        calculate_nav_series(
            [
                {"date": date(2026, 1, 1), "unit_nav": Decimal(1)},
                {"date": date(2026, 1, 1), "unit_nav": Decimal("1.1")},
            ]
        )


def test_nav_series_does_not_carry_total_return_across_missing_last_value() -> None:
    result = calculate_nav_series(
        [
            {"date": date(2026, 1, 1), "unit_nav": Decimal(1)},
            {"date": date(2026, 1, 2), "unit_nav": None},
        ]
    )

    assert result.points[0].cumulative_return == Decimal(0)
    assert result.points[1].cumulative_return is None
    assert result.total_return is None
