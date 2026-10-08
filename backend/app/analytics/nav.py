"""Net asset value return calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ._common import dated_records, decimal, field

ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class NavMetric:
    valuation_date: date
    unit_nav: Decimal | None
    cumulative_unit_nav: Decimal | None
    cumulative_payout: Decimal | None
    adjusted_nav: Decimal | None
    daily_return: Decimal | None
    cumulative_return: Decimal | None
    methodology: str


@dataclass(frozen=True, slots=True)
class NavSeriesResult:
    points: tuple[NavMetric, ...]
    total_return: Decimal | None
    methodology: str


def calculate_nav_series(records: list[Any] | tuple[Any, ...]) -> NavSeriesResult:
    """Calculate total-return NAV metrics from published valuation records.

    Cumulative unit NAV is preferred when it is present for every record.  When
    it is incomplete, unit NAV plus cumulative payout is used as a degraded
    fallback.  A missing payout remains missing; it is never treated as zero.
    Records are sorted by date and duplicate dates are rejected.
    """

    dated = dated_records(records)
    if not dated:
        return NavSeriesResult(points=(), total_return=None, methodology="empty")

    values = []
    for valuation_day, record in dated:
        values.append(
            (
                valuation_day,
                decimal(field(record, "unit_nav")),
                decimal(field(record, "cumulative_unit_nav")),
                decimal(field(record, "cumulative_payout", "payout")),
            )
        )

    has_complete_cumulative = all(item[2] is not None for item in values)
    has_any_payout = any(item[3] is not None for item in values)
    if has_complete_cumulative:
        methodology = "cumulative_unit_nav"
        adjusted_values = [item[2] for item in values]
    else:
        has_complete_unit = all(item[1] is not None for item in values)
        methodology = (
            "unit_nav_plus_cumulative_payout" if has_any_payout else "unit_nav"
        )
        if not has_complete_unit:
            methodology = f"{methodology}_incomplete"
        adjusted_values = [
            None
            if unit_nav is None or (has_any_payout and payout is None)
            else unit_nav + (payout or Decimal(0))
            for _, unit_nav, _, payout in values
        ]

    points: list[NavMetric] = []
    baseline: Decimal | None = None
    previous_adjusted: Decimal | None = None
    for (valuation_day, unit_nav, cumulative_nav, payout), adjusted_nav in zip(
        values, adjusted_values
    ):
        daily_return = None
        if (
            previous_adjusted is not None
            and previous_adjusted != 0
            and adjusted_nav is not None
        ):
            daily_return = adjusted_nav / previous_adjusted - ONE
        if baseline is None and adjusted_nav is not None:
            baseline = adjusted_nav
        cumulative_return = None
        if baseline not in (None, Decimal(0)) and adjusted_nav is not None:
            cumulative_return = adjusted_nav / baseline - ONE
        points.append(
            NavMetric(
                valuation_date=valuation_day,
                unit_nav=unit_nav,
                cumulative_unit_nav=cumulative_nav,
                cumulative_payout=payout,
                adjusted_nav=adjusted_nav,
                daily_return=daily_return,
                cumulative_return=cumulative_return,
                methodology=methodology,
            )
        )
        previous_adjusted = adjusted_nav

    total_return = points[-1].cumulative_return
    return NavSeriesResult(
        points=tuple(points), total_return=total_return, methodology=methodology
    )


def calculate_cumulative_return(
    records: list[Any] | tuple[Any, ...],
) -> NavSeriesResult:
    """Compatibility name for callers interested in the complete NAV result."""

    return calculate_nav_series(records)
