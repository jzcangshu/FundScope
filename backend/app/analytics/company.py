"""Company-level linked index using previous-day net asset weights."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ._common import decimal, field, valuation_date


@dataclass(frozen=True, slots=True)
class CompanyMetric:
    valuation_date: date
    company_index: Decimal | None
    company_daily_return: Decimal | None
    effective_fund_count: int
    coverage: Decimal | None
    total_net_assets: Decimal | None


def calculate_company_index(
    records: list[Any] | tuple[Any, ...],
    *,
    fund_ids: list[Any] | tuple[Any, ...] | None = None,
) -> tuple[CompanyMetric, ...]:
    """Build a date series without carrying a missing fund forward.

    The previous date is the immediately preceding valuation date present in
    the input.  A fund contributes only when it has both records and a daily
    return on the current date, plus a non-null previous net asset value.

    A day without a computable company return leaves the index null for that
    day only; the series resumes from the last valid index on the next
    computable day instead of staying broken.
    """

    expected_ids = list(fund_ids) if fund_ids is not None else []
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("fund_ids contains duplicates")
    grouped: dict[date, dict[Any, tuple[Decimal | None, Decimal | None]]] = {}
    all_ids: set[Any] = set(expected_ids)
    for record in records:
        raw_date = field(record, "valuation_date", "date")
        raw_fund_id = field(record, "fund_id", "fund")
        if raw_date is None or raw_fund_id is None:
            raise ValueError("company record requires valuation_date and fund_id")
        day = valuation_date(raw_date)
        if raw_fund_id in grouped.setdefault(day, {}):
            raise ValueError(
                f"duplicate fund record: {raw_fund_id!r} on {day.isoformat()}"
            )
        nav = decimal(field(record, "net_asset_value", "nav"))
        daily_return = decimal(field(record, "daily_return", "return"))
        grouped[day][raw_fund_id] = (nav, daily_return)
        all_ids.add(raw_fund_id)

    if fund_ids is None:
        expected_ids = sorted(all_ids, key=str)
    expected_count = len(expected_ids)
    days = sorted(grouped)
    if not days:
        return ()

    last_index: Decimal | None = Decimal("1.0000")
    result: list[CompanyMetric] = []
    for position, day in enumerate(days):
        current = grouped[day]
        current_assets = [
            asset
            for fund_id in expected_ids
            if fund_id in current and (asset := current[fund_id][0]) is not None
        ]
        total_net_assets = sum(current_assets, Decimal(0)) if current_assets else None
        if position == 0:
            available_count = sum(
                fund_id in current and current[fund_id][0] is not None
                for fund_id in expected_ids
            )
            result.append(
                CompanyMetric(
                    day,
                    Decimal("1.0000"),
                    None,
                    available_count,
                    Decimal(available_count) / Decimal(expected_count)
                    if expected_count
                    else None,
                    total_net_assets,
                )
            )
            continue

        previous = grouped[days[position - 1]]
        previous_assets = [
            asset
            for fund_id in expected_ids
            if fund_id in previous and (asset := previous[fund_id][0]) is not None
        ]
        previous_total = sum(previous_assets, Decimal(0))
        eligible = [
            fund_id
            for fund_id in expected_ids
            if previous_total != 0
            and fund_id in previous
            and fund_id in current
            and previous[fund_id][0] is not None
            and current[fund_id][1] is not None
        ]
        weighted_returns: list[Decimal] = []
        for fund_id in eligible:
            previous_nav = previous[fund_id][0]
            daily_return = current[fund_id][1]
            if previous_nav is not None and daily_return is not None:
                weighted_returns.append((previous_nav / previous_total) * daily_return)
        company_return = None
        company_index = None
        if weighted_returns and previous_total != 0:
            company_return = sum(weighted_returns, Decimal(0))
            if last_index is not None:
                company_index = last_index * (Decimal(1) + company_return)
        # An uncomputable day leaves the index null but must not kill the
        # chain: the next computable day resumes from the last valid index.
        if company_index is not None:
            last_index = company_index
        result.append(
            CompanyMetric(
                valuation_date=day,
                company_index=company_index,
                company_daily_return=company_return,
                effective_fund_count=len(eligible),
                coverage=(
                    Decimal(len(eligible)) / Decimal(expected_count)
                    if expected_count
                    else None
                ),
                total_net_assets=total_net_assets,
            )
        )
    return tuple(result)
