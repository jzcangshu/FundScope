"""Compute portfolio history from a user's authorized published funds only."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import TypedDict, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.company import CompanyMetric, calculate_company_index
from app.analytics.nav import calculate_nav_series
from app.db.base import FundStatus, ValuationStatus
from app.db.models import Fund, FundDailySnapshot, ValuationVersion


class _ScopeRecord(TypedDict):
    fund_id: int
    valuation_date: date
    net_asset_value: Decimal | None
    unit_nav: Decimal | None
    cumulative_unit_nav: Decimal | None
    cumulative_payout: Decimal | None


def scoped_company_history(
    session: Session, fund_ids: frozenset[int], *, end: date | None = None
) -> tuple[tuple[CompanyMetric, ...], dict[date, Decimal | None]]:
    """Use the same NAV and weighting formulas as the company analysis worker.

    Read scalar columns rather than ORM snapshots/run histories. Keep the
    authorized funds' full past to preserve the index base across date windows.
    Global company metrics and their dates must never enter this calculation.
    """
    active_ids = tuple(
        session.scalars(
            select(Fund.id).where(
                Fund.id.in_(fund_ids), Fund.status == FundStatus.ACTIVE
            )
        )
    )
    if not active_ids:
        return (), {}
    statement = (
        select(
            ValuationVersion.fund_id,
            ValuationVersion.valuation_date,
            FundDailySnapshot.net_asset_value,
            FundDailySnapshot.unit_nav,
            FundDailySnapshot.cumulative_unit_nav,
            FundDailySnapshot.cumulative_payout,
        )
        .join(
            FundDailySnapshot,
            FundDailySnapshot.valuation_version_id == ValuationVersion.id,
        )
        .where(
            ValuationVersion.fund_id.in_(active_ids),
            ValuationVersion.status == ValuationStatus.PUBLISHED,
        )
        .order_by(ValuationVersion.fund_id, ValuationVersion.valuation_date)
    )
    if end is not None:
        statement = statement.where(ValuationVersion.valuation_date <= end)
    grouped: dict[int, list[_ScopeRecord]] = defaultdict(list)
    for row in session.execute(statement.execution_options(yield_per=1000)).mappings():
        grouped[row["fund_id"]].append(cast(_ScopeRecord, dict(row)))
    inputs: list[dict[str, object]] = []
    updates: dict[date, dict[int, Decimal]] = defaultdict(dict)
    for fund_id, records in grouped.items():
        nav = calculate_nav_series(records)
        returns = {point.valuation_date: point.daily_return for point in nav.points}
        for row in records:
            day = row["valuation_date"]
            inputs.append({**row, "daily_return": returns[day]})
            net_assets = row["net_asset_value"]
            if net_assets is not None:
                updates[day][fund_id] = net_assets
    metrics = calculate_company_index(inputs, fund_ids=active_ids)
    latest_assets: dict[int, Decimal] = {}
    total_assets: dict[date, Decimal | None] = {}
    for metric in metrics:
        latest_assets.update(updates.get(metric.valuation_date, {}))
        total_assets[metric.valuation_date] = (
            sum(latest_assets.values(), Decimal(0)) if latest_assets else None
        )
    return metrics, total_assets
