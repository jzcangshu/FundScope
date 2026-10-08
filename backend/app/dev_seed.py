"""Create deterministic local-only demo data for the fund dashboard.

This module is intentionally separate from application startup. It writes only
funds marked with the ``DEMO-`` product-code prefix and refuses to run when
those records already exist, so a developer cannot accidentally overwrite an
existing local database by restarting the API.

Run from the ``backend`` directory after applying migrations::

    ..\\.venv\\Scripts\\python.exe -m app.dev_seed
"""

from __future__ import annotations

import argparse
import random
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.company import calculate_company_index
from app.config import get_settings
from app.db.base import (
    AnalysisRunStatus,
    AuditResult,
    FundStatus,
    RiskEventStatus,
    RiskSeverity,
    ValidationLevel,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    AuditLog,
    CompanyMetricDaily,
    Fund,
    FundDailySnapshot,
    FundMetricDaily,
    PositionDaily,
    RiskEvent,
    RiskRule,
    ValidationResult,
    ValuationVersion,
)
from app.db.models.benchmark import BenchmarkDaily, BenchmarkSyncState
from app.db.session import create_engine

DEMO_PREFIX = "DEMO-"
HISTORY_DAYS = 90
# The simulated CSI-300 proxy covers about two trading years so the fund
# detail benchmark row can show 近三月 / 近一年 comparisons, not just the
# last 90 days.
BENCHMARK_DAYS = 400
MONEY_QUANTUM = Decimal("0.01")
NAV_QUANTUM = Decimal("0.0000000001")

# Fictional products used only by the local demo seed.  None of these names
# correspond to a real fund; they exist so screenshots and local UI checks have
# something to render.
FUND_DEFINITIONS = (
    ("示例成长精选私募证券投资基金", "成长精选", "成长精选"),
    ("示例科技先锋私募证券投资基金", "科技先锋", "科技成长"),
    ("示例价值发现私募证券投资基金", "价值发现", "价值投资"),
    ("示例量化多策略私募证券投资基金", "量化多策略", "量化多策略"),
    ("示例蓝筹精选私募证券投资基金", "蓝筹精选", "股票多头"),
    ("示例稳健增利私募证券投资基金", "稳健增利", "固收+"),
    ("示例星河成长私募证券投资基金", "星河成长", "成长精选"),
    ("示例宏观对冲私募证券投资基金", "宏观对冲", "宏观策略"),
    ("示例多资产配置私募证券投资基金", "多资产配置", "多资产"),
    ("示例新经济私募证券投资基金", "新经济", "科技成长"),
    ("示例长期价值私募证券投资基金", "长期价值", "价值投资"),
    ("示例指数增强私募证券投资基金", "指数增强", "量化多策略"),
    ("示例行业轮动私募证券投资基金", "行业轮动", "股票多头"),
    ("示例固收增强私募证券投资基金", "固收增强", "固收+"),
    ("示例全球配置私募证券投资基金", "全球配置", "多资产"),
)

POSITION_DEFINITIONS = (
    ("示例光电子", "XSE301", "深圳", "0.24", "135.40", "0.08"),
    ("示例集成电路", "XSE302", "上海", "0.18", "98.20", "0.04"),
    ("示例智能计算", "XSE303", "上海", "0.16", "83.70", "0.06"),
    ("示例光通信", "XSE304", "深圳", "0.12", "31.65", "0.03"),
    ("示例商业银行", "XSE305", "上海", "0.10", "36.20", "0.02"),
    ("现金及收益互换", "OTHER", "场外", "0.08", "1.00", "0.00"),
)

# (beta vs. the simulated CSI-300 benchmark, idiosyncratic daily vol, daily drift).
# Equity strategies track the benchmark with meaningful tracking error so the
# comparison chart and drawdowns look like real fund data; fixed-income
# strategies barely move with equities.
FUND_PROFILES = (
    (0.85, 0.0100, 0.0010),  # 成长精选
    (1.00, 0.0130, 0.0018),  # 科技先锋
    (0.70, 0.0075, 0.0008),  # 价值发现
    (0.60, 0.0085, 0.0010),  # 量化多策略
    (0.80, 0.0085, 0.0007),  # 蓝筹精选
    (0.05, 0.0015, 0.00035),  # 稳健增利
    (0.90, 0.0110, 0.0011),  # 星河成长
    (0.30, 0.0065, 0.0006),  # 宏观对冲
    (0.45, 0.0055, 0.0005),  # 多资产配置
    (0.95, 0.0120, 0.0015),  # 新经济
    (0.70, 0.0065, 0.0006),  # 长期价值
    (0.98, 0.0035, 0.0009),  # 指数增强
    (0.80, 0.0100, 0.0009),  # 行业轮动
    (0.08, 0.0018, 0.0004),  # 固收增强
    (0.40, 0.0050, 0.0005),  # 全球配置
)


def _simulate_demo_series(latest_date: date) -> tuple[list[float], list[list[float]]]:
    """Deterministic simulated daily log-returns.

    Returns ``(benchmark_returns, fund_returns)`` keyed by ``latest_date`` so
    repeated seeding produces identical demo data.  The CSI-300 proxy covers
    ``BENCHMARK_DAYS`` (so comparison periods beyond 90 days have data); each
    fund's 90-day series is ``beta * benchmark_window + drift + idiosyncratic
    noise``, which keeps the comparison charts and drawdown numbers plausible.
    """

    rng = random.Random(latest_date.toordinal() * 7919 + 13)
    benchmark = [0.00045 + rng.gauss(0.0, 0.0108) for _ in range(BENCHMARK_DAYS)]
    benchmark_window = benchmark[-HISTORY_DAYS:]
    funds: list[list[float]] = []
    for beta, idio_vol, drift in FUND_PROFILES:
        funds.append(
            [
                beta * b_ret + drift + rng.gauss(0.0, idio_vol)
                for b_ret in benchmark_window
            ]
        )
    return benchmark, funds


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _nav(value: Decimal) -> Decimal:
    return value.quantize(NAV_QUANTUM, rounding=ROUND_HALF_UP)


def _initial_nav(index: int) -> Decimal:
    if index == 0:
        return Decimal("1.4200")
    if index == 1:
        return Decimal("2.6800")
    return Decimal("1.0800") + Decimal(index) * Decimal("0.0550")


def _base_assets(index: int) -> Decimal:
    if index == 0:
        return Decimal(908000000)
    if index == 1:
        return Decimal(187000000)
    return Decimal(320000000) + Decimal(index - 2) * Decimal(35000000)


def _seed_benchmark(
    session: Session,
    *,
    latest_date: date,
    now: datetime,
    returns: list[float],
) -> None:
    """Seed the simulated CSI-300 proxy used by the comparison charts.

    Skips when benchmark rows already exist (a developer may have run the
    real Tushare sync against a local database).
    """

    if session.scalar(select(BenchmarkDaily.id).limit(1)) is not None:
        return
    start_date = latest_date - timedelta(days=BENCHMARK_DAYS - 1)
    close = Decimal("3860.00")
    for offset, log_return in enumerate(returns):
        trade_date = start_date + timedelta(days=offset)
        close = _money(close * (Decimal(1) + Decimal(repr(log_return))))
        session.add(
            BenchmarkDaily(
                ts_code="000300.SH",
                trade_date=trade_date,
                close=close,
                pct_chg=_nav(Decimal(repr(log_return))),
                source="demo_seed",
            )
        )
    session.add(
        BenchmarkSyncState(
            id=1,
            ts_code="000300.SH",
            last_attempt_at=now,
            last_success_at=now,
            last_trade_date=latest_date,
            earliest_trade_date=start_date,
            total_rows=BENCHMARK_DAYS,
            last_error=None,
        )
    )


def _seed_fund_history(
    session: Session,
    fund: Fund,
    *,
    index: int,
    latest_date: date,
    daily_returns: list[float],
) -> tuple[ValuationVersion, ...]:
    start_date = latest_date - timedelta(days=HISTORY_DAYS - 1)
    initial_nav = _initial_nav(index)
    previous_nav = initial_nav
    nav_by_date: dict[date, Decimal] = {}
    versions: list[ValuationVersion] = []

    for offset in range(HISTORY_DAYS):
        valuation_date = start_date + timedelta(days=offset)
        prior_nav = previous_nav
        current_nav = (
            _nav(prior_nav * (Decimal(1) + Decimal(repr(daily_returns[offset]))))
            if offset
            else _nav(prior_nav)
        )
        daily_return = _nav(current_nav / prior_nav - Decimal(1)) if offset else None
        previous_nav = current_nav
        nav_by_date[valuation_date] = current_nav
        week_start = valuation_date - timedelta(days=valuation_date.weekday())
        quarter_start = date(
            valuation_date.year,
            ((valuation_date.month - 1) // 3) * 3 + 1,
            1,
        )
        period_returns = {
            "wtd_return": _nav(
                current_nav / nav_by_date.get(week_start, initial_nav) - Decimal(1)
            ),
            "mtd_return": _nav(
                current_nav
                / nav_by_date.get(valuation_date.replace(day=1), initial_nav)
                - Decimal(1)
            ),
            "qtd_return": _nav(
                current_nav / nav_by_date.get(quarter_start, initial_nav) - Decimal(1)
            ),
            "ytd_return": _nav(
                current_nav
                / nav_by_date.get(valuation_date.replace(month=1, day=1), initial_nav)
                - Decimal(1)
            ),
            "cumulative_return": _nav(current_nav / initial_nav - Decimal(1)),
        }
        # AUM tracks the NAV path so the company overview chart moves with the
        # market instead of growing on a straight line.
        total_assets = _money(_base_assets(index) * (current_nav / initial_nav))
        liabilities = _money(total_assets * Decimal("0.035"))
        net_assets = _money(total_assets - liabilities)
        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=valuation_date,
            version_no=1,
            status=ValuationStatus.PUBLISHED,
            published_by="demo-seed",
            published_at=datetime.combine(
                valuation_date, datetime.min.time(), tzinfo=UTC
            ),
            release_reason="本地演示数据",
        )
        session.add(version)
        session.flush()
        session.add(
            FundDailySnapshot(
                valuation_version_id=version.id,
                total_assets=total_assets,
                total_liabilities=liabilities,
                net_asset_value=net_assets,
                unit_nav=current_nav,
                cumulative_unit_nav=current_nav,
                previous_unit_nav=prior_nav if offset else None,
                daily_return=daily_return,
                **period_returns,
            )
        )
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="DEMO-CHECK",
                level=ValidationLevel.INFO,
                message="演示数据已通过基础校验",
            )
        )
        versions.append(version)

    session.flush()
    return tuple(versions)


def _seed_positions(session: Session, version: ValuationVersion, index: int) -> None:
    snapshot = session.scalar(
        select(FundDailySnapshot).where(
            FundDailySnapshot.valuation_version_id == version.id
        )
    )
    if snapshot is None or snapshot.net_asset_value is None:
        return
    for position_index, (name, code, market, weight, price, gain) in enumerate(
        POSITION_DEFINITIONS
    ):
        nav_weight = Decimal(weight) + Decimal(index % 3) / Decimal(1000)
        market_value = _money(snapshot.net_asset_value * nav_weight)
        market_price = Decimal(price)
        quantity = _money(market_value / market_price)
        session.add(
            PositionDaily(
                valuation_version_id=version.id,
                security_code=code,
                security_name=name,
                market=market,
                account=f"演示组合-{index + 1:02d}",
                quantity=quantity,
                market_price=market_price,
                market_value=market_value,
                nav_weight=nav_weight,
                valuation_gain=_money(market_value * Decimal(gain)),
                source_worksheet="demo_seed",
                source_row=position_index + 2,
            )
        )


def _seed_analysis(
    session: Session,
    fund: Fund,
    versions: tuple[ValuationVersion, ...],
    *,
    index: int,
    latest_date: date,
    now: datetime,
) -> AnalysisRun:
    run = AnalysisRun(
        trigger_version_id=versions[-1].id,
        trigger_reason="demo_seed",
        input_start_date=versions[0].valuation_date,
        input_end_date=latest_date,
        input_version_range=f"fund:{fund.id};demo",
        methodology_version="demo-v1",
        status=AnalysisRunStatus.SUCCEEDED,
        started_at=now,
        ended_at=now,
    )
    session.add(run)
    session.flush()
    peak_nav = Decimal(0)
    for version in versions:
        snapshot = session.scalar(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id == version.id
            )
        )
        if snapshot is None or snapshot.unit_nav is None:
            continue
        peak_nav = max(peak_nav, snapshot.unit_nav)
        session.add(
            FundMetricDaily(
                fund_id=fund.id,
                valuation_date=version.valuation_date,
                source_analysis_run_id=run.id,
                daily_return=snapshot.daily_return,
                cumulative_return=_nav(
                    snapshot.unit_nav / _initial_nav(index) - Decimal(1)
                ),
                drawdown=_nav(snapshot.unit_nav / peak_nav - Decimal(1)),
                historical_peak=peak_nav,
                concentration=Decimal("0.24"),
                asset_ratio=Decimal("0.965"),
            )
        )
    session.flush()
    return run


def seed_demo_data(session: Session, *, latest_date: date) -> int:
    existing = session.scalars(
        select(Fund).where(Fund.product_code.like(f"{DEMO_PREFIX}%"))
    ).all()
    if existing:
        print(f"已找到 {len(existing)} 只 DEMO 基金，跳过写入。")
        return 0

    now = datetime.now(UTC)
    benchmark_returns, fund_returns = _simulate_demo_series(latest_date)
    _seed_benchmark(
        session, latest_date=latest_date, now=now, returns=benchmark_returns
    )
    funds: list[Fund] = []
    histories: dict[int, tuple[ValuationVersion, ...]] = {}
    for index, (name, _, strategy) in enumerate(FUND_DEFINITIONS):
        fund = Fund(
            standard_name=name,
            product_code=f"{DEMO_PREFIX}{index + 1:02d}",
            establishment_date=date(2021 + index % 4, 1 + index % 10, 1),
            strategy=strategy,
            manager="示例资产管理团队",
            status=FundStatus.ACTIVE,
            notes="本地演示数据，仅用于界面验证，不代表真实投资结果。",
        )
        session.add(fund)
        session.flush()
        funds.append(fund)
        histories[fund.id] = _seed_fund_history(
            session,
            fund,
            index=index,
            latest_date=latest_date,
            daily_returns=fund_returns[index],
        )
        _seed_positions(session, histories[fund.id][-1], index)

    session.flush()
    runs = [
        _seed_analysis(
            session,
            fund,
            histories[fund.id],
            index=index,
            latest_date=latest_date,
            now=now,
        )
        for index, fund in enumerate(funds)
    ]

    company_inputs: list[dict[str, object]] = []
    for fund, history in zip(funds, histories.values(), strict=True):
        for version in history:
            snapshot = session.scalar(
                select(FundDailySnapshot).where(
                    FundDailySnapshot.valuation_version_id == version.id
                )
            )
            if snapshot is None:
                continue
            company_inputs.append(
                {
                    "fund_id": fund.id,
                    "valuation_date": version.valuation_date,
                    "net_asset_value": snapshot.net_asset_value,
                    "daily_return": snapshot.daily_return,
                }
            )
    company_metrics = calculate_company_index(
        company_inputs,
        fund_ids=[fund.id for fund in funds],
    )
    session.add_all(
        CompanyMetricDaily(
            valuation_date=metric.valuation_date,
            source_analysis_run_id=runs[0].id,
            company_index=metric.company_index,
            company_daily_return=metric.company_daily_return,
            effective_fund_count=metric.effective_fund_count,
            total_net_assets=metric.total_net_assets,
        )
        for metric in company_metrics
    )

    risk_rule_definitions = (
        ("DEMO-DRAWDOWN", "最大回撤", RiskSeverity.CRITICAL, Decimal("0.10")),
        ("DEMO-CONCENTRATION", "单一持仓集中度", RiskSeverity.WARNING, Decimal("0.25")),
        ("DEMO-DAILY-LOSS", "单日亏损", RiskSeverity.WARNING, Decimal("0.03")),
    )
    risk_rules: list[RiskRule] = []
    for code, rule_type, severity, threshold in risk_rule_definitions:
        rule = RiskRule(
            rule_code=code,
            rule_type=rule_type,
            scope="fund",
            threshold=threshold,
            severity=severity,
            valid_from=latest_date - timedelta(days=HISTORY_DAYS),
            version="demo-v1",
            enabled=True,
        )
        session.add(rule)
        risk_rules.append(rule)
    session.flush()
    for rule, fund_index in zip(risk_rules, (2, 1, 4), strict=False):
        fund = funds[fund_index]
        session.add(
            RiskEvent(
                risk_rule_id=rule.id,
                fund_id=fund.id,
                valuation_date=latest_date,
                severity=rule.severity,
                status=RiskEventStatus.OPEN,
                first_triggered_at=now,
                last_triggered_at=now,
                evidence_snapshot="演示风险事件，用于检查风险列表与详情跳转。",
                evidence_reference="demo_seed",
            )
        )
    session.add(
        AuditLog(
            actor_user_id=None,
            action="demo.seed",
            resource_type="database",
            resource_id="local",
            summary={"fund_count": len(funds), "history_days": HISTORY_DAYS},
            reason="本地演示数据",
            result=AuditResult.SUCCESS,
        )
    )
    session.commit()
    return len(funds)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式应为 YYYY-MM-DD") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="写入本地基金看板演示数据")
    parser.add_argument(
        "--latest-date",
        type=_parse_date,
        default=date(2026, 8, 17),
        help="演示数据最新估值日，默认 2026-08-17",
    )
    args = parser.parse_args()
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        count = seed_demo_data(session, latest_date=args.latest_date)
    if count:
        print(f"已写入 {count} 只基金、{count * HISTORY_DAYS} 个估值版本和最新持仓。")


if __name__ == "__main__":
    main()
