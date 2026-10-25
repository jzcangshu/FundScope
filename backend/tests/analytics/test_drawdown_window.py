from datetime import date
from decimal import Decimal

import pytest

from app.analytics.drawdown import (
    STATUS_INCOMPLETE,
    STATUS_INSUFFICIENT,
    STATUS_INVALID,
    STATUS_OK,
    calculate_drawdown,
    calculate_range_drawdown,
    calendar_start_date,
)


def points(values, start: date = date(2026, 1, 1)):
    return [
        {
            "valuation_date": date(start.year, start.month, start.day)
            if index == 0
            else _add_days(start, index),
            "adjusted_nav": None if value is None else Decimal(value),
        }
        for index, value in enumerate(values)
    ]


def _add_days(start: date, days: int) -> date:
    from datetime import timedelta

    return start + timedelta(days=days)


def dated(values: dict[str, str | None]) -> list[dict]:
    return [
        {
            "valuation_date": date.fromisoformat(day),
            "adjusted_nav": None if value is None else Decimal(value),
        }
        for day, value in values.items()
    ]


# ---------------------------------------------------------------------------
# Acceptance examples
# ---------------------------------------------------------------------------


def test_example_a_uses_the_running_peak_not_the_period_open() -> None:
    result = calculate_range_drawdown(points(["1.00", "1.20", "1.10"]))

    assert result.status == STATUS_OK
    assert [point.drawdown for point in result.points] == [
        Decimal(0),
        Decimal(0),
        Decimal("-0.0833333333333333333333333333"),
    ]
    assert result.current_drawdown == Decimal("-0.0833333333333333333333333333")
    assert result.max_drawdown == Decimal("-0.0833333333333333333333333333")
    # Rejected alternatives: -10% (period-open denominator) and +10% (return).
    assert result.max_drawdown != Decimal("-0.1")


def test_example_b_full_window_and_reset_on_a_narrower_window() -> None:
    series = points(["1.00", "1.20", "0.90", "1.10"])
    full = calculate_range_drawdown(series)

    assert [point.drawdown for point in full.points] == [
        Decimal(0),
        Decimal(0),
        Decimal("-0.25"),
        Decimal("-0.0833333333333333333333333333"),
    ]
    assert full.max_drawdown == Decimal("-0.25")
    assert full.current_drawdown == Decimal("-0.0833333333333333333333333333")

    sub = calculate_range_drawdown(series, start=date(2026, 1, 3))
    assert [point.drawdown for point in sub.points] == [Decimal(0), Decimal(0)]
    assert sub.current_drawdown == Decimal(0)
    assert sub.max_drawdown == Decimal(0)
    assert sub.start == date(2026, 1, 3)
    assert sub.end == date(2026, 1, 4)


def test_example_c_current_and_maximum_are_not_interchangeable() -> None:
    result = calculate_range_drawdown(points(["1.00", "1.20", "1.05", "1.15"]))

    assert result.current_drawdown == Decimal("-0.0416666666666666666666666667")
    assert result.max_drawdown == Decimal("-0.125")


def test_example_d_a_later_high_never_pairs_with_an_earlier_low() -> None:
    result = calculate_range_drawdown(points(["1.00", "0.90", "1.20"]))

    assert result.current_drawdown == Decimal(0)
    assert result.max_drawdown == Decimal("-0.1")


def test_example_e_highs_before_the_window_never_participate() -> None:
    result = calculate_range_drawdown(
        points(["2.00", "1.00", "1.10"]), start=date(2026, 1, 2)
    )

    assert [point.drawdown for point in result.points] == [Decimal(0), Decimal(0)]
    assert result.current_drawdown == Decimal(0)
    assert result.max_drawdown == Decimal(0)


def test_example_f_normalising_the_series_does_not_change_the_result() -> None:
    raw = points(["1.00", "1.20", "0.90", "1.10"])
    base = Decimal("1.00")
    normalised = [
        {
            "valuation_date": item["valuation_date"],
            "adjusted_nav": item["adjusted_nav"] / base,
        }
        for item in raw
    ]

    assert calculate_range_drawdown(normalised).max_drawdown == Decimal("-0.25")
    assert (
        calculate_range_drawdown(normalised).current_drawdown
        == calculate_range_drawdown(raw).current_drawdown
    )


# ---------------------------------------------------------------------------
# Window resolution and missing data
# ---------------------------------------------------------------------------


def test_empty_series_is_unavailable() -> None:
    result = calculate_range_drawdown([])

    assert result.status == STATUS_INSUFFICIENT
    assert result.current_drawdown is None
    assert result.max_drawdown is None


def test_single_point_is_a_flat_window() -> None:
    result = calculate_range_drawdown(points(["1.40"]))

    assert result.status == STATUS_OK
    assert result.current_drawdown == Decimal(0)
    assert result.max_drawdown == Decimal(0)


@pytest.mark.parametrize("value", [None, "0", "-1", "NaN", "Infinity"])
def test_invalid_baseline_is_reported_not_skipped(value: str | None) -> None:
    series = points(["1.00", value, "1.10"])
    result = calculate_range_drawdown(series, start=date(2026, 1, 2))

    assert result.status == STATUS_INVALID
    assert result.reason is not None and "区间起点" in result.reason
    assert result.current_drawdown is None
    assert result.max_drawdown is None


def test_missing_middle_point_breaks_the_series_and_flags_incomplete() -> None:
    result = calculate_range_drawdown(points(["1.00", "1.20", None, "0.96"]))

    assert result.status == STATUS_INCOMPLETE
    assert result.points[2].drawdown is None
    # The peak observed before the gap still applies afterwards.
    assert result.points[3].drawdown == Decimal("-0.2")
    assert result.missing_points == 1


def test_missing_end_point_leaves_only_the_current_drawdown_empty() -> None:
    result = calculate_range_drawdown(points(["1.00", "1.20", "0.90", None]))

    assert result.current_drawdown is None
    assert result.max_drawdown == Decimal("-0.25")
    assert result.status == STATUS_INCOMPLETE


def test_end_date_without_a_valuation_is_unavailable() -> None:
    result = calculate_range_drawdown(points(["1.00", "1.10"]), end=date(2026, 1, 5))

    assert result.status == STATUS_INSUFFICIENT
    assert result.reason is not None and "没有已发布的净值记录" in result.reason


def test_baseline_is_the_last_valuation_on_or_before_the_natural_day() -> None:
    result = calculate_range_drawdown(
        dated({"2026-09-01": "1.00", "2026-09-09": "1.25", "2026-09-16": "1.10"}),
        start=date(2026, 9, 7),
        end=date(2026, 9, 16),
    )

    assert result.target_start == date(2026, 9, 7)
    assert result.start == date(2026, 9, 1)
    assert result.end == date(2026, 9, 16)
    assert result.current_drawdown == Decimal("-0.12")
    assert result.max_drawdown == Decimal("-0.12")


def test_insufficient_history_is_not_replaced_by_a_later_baseline() -> None:
    result = calculate_range_drawdown(
        dated({"2026-09-10": "1.00", "2026-09-16": "1.10"}),
        start=date(2026, 9, 1),
        end=date(2026, 9, 16),
    )

    assert result.status == STATUS_INSUFFICIENT
    assert result.reason is not None and "历史不足" in result.reason
    assert result.current_drawdown is None


def test_end_before_the_window_start_is_unavailable() -> None:
    result = calculate_range_drawdown(
        dated({"2025-01-10": "1.00", "2025-01-20": "1.10"}),
        start=date(2026, 1, 1),
        end=date(2025, 1, 20),
    )

    assert result.status == STATUS_INSUFFICIENT
    assert result.reason is not None and "所选区间内没有估值记录" in result.reason


@pytest.mark.parametrize("value", [None, "0", "-1", "NaN", "Infinity"])
def test_partial_start_never_skips_an_invalid_first_value(value: str | None) -> None:
    result = calculate_range_drawdown(
        dated({"2026-01-05": value, "2026-01-06": "1.10"}),
        start=date(2026, 1, 1),
        allow_partial_start=True,
    )
    assert result.status == STATUS_INVALID
    assert result.max_drawdown is None


def test_partial_start_retains_gaps_and_reports_both_limitations() -> None:
    result = calculate_range_drawdown(
        dated({"2026-01-05": "1.20", "2026-01-06": None, "2026-01-07": "1.08"}),
        start=date(2026, 1, 1),
        allow_partial_start=True,
    )
    assert result.status == STATUS_INCOMPLETE
    assert result.start == date(2026, 1, 5)
    assert result.max_drawdown == Decimal("-0.1")
    assert result.points[1].drawdown is None
    assert result.missing_points == 1
    assert result.reason is not None
    assert "不代表完整区间" in result.reason
    assert "缺失或无效" in result.reason


def test_partial_start_still_requires_the_exact_end_valuation() -> None:
    result = calculate_range_drawdown(
        dated({"2026-01-05": "1.20", "2026-01-06": "1.08"}),
        start=date(2026, 1, 1),
        end=date(2026, 1, 7),
        allow_partial_start=True,
    )
    assert result.status == STATUS_INSUFFICIENT
    assert result.current_drawdown is None


def test_peak_and_trough_dates_explain_the_extreme() -> None:
    result = calculate_range_drawdown(points(["1.00", "1.20", "0.90", "1.10"]))

    assert result.peak_date == date(2026, 1, 2)
    assert result.trough_date == date(2026, 1, 3)
    assert result.peak_value == Decimal("1.20")
    assert result.trough_value == Decimal("0.90")


# ---------------------------------------------------------------------------
# Calendar windows
# ---------------------------------------------------------------------------


def test_calendar_start_clamps_month_ends() -> None:
    assert calendar_start_date(date(2024, 3, 31), "1m") == date(2024, 2, 29)
    assert calendar_start_date(date(2025, 3, 31), "1m") == date(2025, 2, 28)
    assert calendar_start_date(date(2024, 5, 31), "3m") == date(2024, 2, 29)
    assert calendar_start_date(date(2024, 2, 29), "1y") == date(2023, 2, 28)
    assert calendar_start_date(date(2026, 1, 4), "1w") == date(2025, 12, 28)
    assert calendar_start_date(date(2026, 9, 15), "ytd") == date(2026, 1, 1)
    assert calendar_start_date(date(2026, 9, 15), "all") is None


def test_legacy_full_history_calculation_is_untouched() -> None:
    """Risk rules and analysis persistence keep the whole-history caliber."""

    result = calculate_drawdown(
        [
            {"date": date(2026, 1, 1), "value": Decimal("1.00")},
            {"date": date(2026, 1, 2), "value": Decimal("1.20")},
            {"date": date(2026, 1, 3), "value": Decimal("0.90")},
        ]
    )

    assert result.max_drawdown == Decimal("-0.25")
    assert result.current_drawdown == Decimal("-0.25")
    assert result.peak_date == date(2026, 1, 2)
