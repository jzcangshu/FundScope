from datetime import date
from decimal import Decimal

from app.analytics.drawdown import calculate_drawdown


def test_drawdown_reports_peak_trough_maximum_and_current_values() -> None:
    result = calculate_drawdown(
        [
            {"date": date(2026, 1, 1), "value": Decimal("1.00")},
            {"date": date(2026, 1, 2), "value": Decimal("1.20")},
            {"date": date(2026, 1, 3), "value": Decimal("0.90")},
            {"date": date(2026, 1, 4), "value": Decimal("1.10")},
            {"date": date(2026, 1, 5), "value": Decimal("0.80")},
        ]
    )

    assert result.max_drawdown == Decimal(-1) / Decimal(3)
    assert result.current_drawdown == Decimal(-1) / Decimal(3)
    assert result.peak_date == date(2026, 1, 2)
    assert result.trough_date == date(2026, 1, 5)
    assert result.points[2].drawdown == Decimal("-0.25")


def test_drawdown_does_not_divide_by_zero_or_fill_missing_value() -> None:
    result = calculate_drawdown(
        [
            {"date": date(2026, 1, 1), "value": Decimal(0)},
            {"date": date(2026, 1, 2), "value": None},
            {"date": date(2026, 1, 3), "value": Decimal(1)},
        ]
    )

    assert result.points[0].drawdown is None
    assert result.points[1].drawdown is None
    assert result.points[2].drawdown == Decimal(0)
    assert result.max_drawdown == Decimal(0)


def test_drawdown_for_all_zero_values_is_unknown() -> None:
    result = calculate_drawdown([{"date": date(2026, 1, 1), "value": Decimal(0)}])

    assert result.current_drawdown is None
    assert result.max_drawdown is None
    assert result.peak_date is None
