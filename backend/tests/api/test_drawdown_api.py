from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from io import StringIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import (
    FundStatus,
    UserRole,
    UserStatus,
    ValidationLevel,
    ValuationStatus,
)
from app.db.models import (
    Fund,
    FundDailySnapshot,
    User,
    ValidationResult,
    ValuationVersion,
)
from app.publishing import PublishingService

NAV_PATH = [
    (date(2026, 6, 1), "1.00"),
    (date(2026, 7, 1), "1.20"),
    (date(2026, 8, 1), "0.90"),
    (date(2026, 9, 1), "1.10"),
]


def make_fund(
    session: Session,
    *,
    name: str = "回撤测试产品",
    establishment_date: date | None = date(2026, 6, 1),
    nav: list[tuple[date, str]] | None = None,
) -> int:
    """Create a fund and publish one valuation per day through the real service."""

    actor = session.scalar(select(User).where(User.username == "admin"))
    if actor is None:
        actor = User(
            username="admin",
            password_hash="not-used",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(actor)
        session.flush()
    fund = Fund(
        standard_name=name,
        status=FundStatus.ACTIVE,
        establishment_date=establishment_date,
    )
    session.add(fund)
    session.flush()
    for version_no, (valuation_day, unit_nav) in enumerate(nav or NAV_PATH, start=1):
        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=valuation_day,
            version_no=version_no,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(version)
        session.flush()
        session.add(
            FundDailySnapshot(
                valuation_version_id=version.id,
                net_asset_value=Decimal(100000),
                unit_nav=Decimal(unit_nav),
                cumulative_unit_nav=Decimal(unit_nav),
            )
        )
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="test_rule",
                level=ValidationLevel.INFO,
                message="校验通过",
            )
        )
        session.commit()
        PublishingService(session).publish_version(
            version.id,
            actor_user_id=actor.id,
            actor_label=actor.username,
            reason="回撤测试发布",
        )
        session.commit()
    return int(fund.id)


def test_fund_drawdowns_endpoint(admin_client, app_and_engine) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        fund = make_fund(session)

    full = admin_client.get("/api/v1/dashboard/fund-drawdowns?range=all").json()["data"]
    row = next(item for item in full["funds"] if item["fund_id"] == fund)
    assert row["start"] == "2026-06-01"
    assert row["end"] == "2026-09-01"
    assert Decimal(row["max_drawdown"]) == Decimal("-0.25")
    assert row["current_drawdown"] is not None
    assert Decimal(row["current_drawdown"]).quantize(Decimal("0.000001")) == Decimal(
        "-0.083333"
    )
    assert row["peak_date"] == "2026-07-01"
    assert row["trough_date"] == "2026-08-01"
    assert row["status"] == "ok"
    assert full["window_note"] is not None  # "all" states the loaded window

    # 近一月 starts at the 2026-08-01 valuation: the June/July high is gone.
    month = admin_client.get("/api/v1/dashboard/fund-drawdowns?range=1m").json()["data"]
    row = next(item for item in month["funds"] if item["fund_id"] == fund)
    assert row["start"] == "2026-08-01"
    assert Decimal(row["current_drawdown"]) == Decimal(0)
    assert Decimal(row["max_drawdown"]) == Decimal(0)


def test_ytd_partial_history_agrees_with_nav_export(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        fund = make_fund(session)

    payload = admin_client.get("/api/v1/dashboard/fund-drawdowns?range=ytd").json()
    row = next(item for item in payload["data"]["funds"] if item["fund_id"] == fund)
    assert row["status"] == "incomplete"
    assert row["target_start"] == "2026-01-01"
    assert row["start"] == "2026-06-01"
    assert "2026-06-01" in row["reason"]
    assert "不代表完整区间" in row["reason"]
    assert Decimal(row["max_drawdown"]) == Decimal("-0.25")

    response = admin_client.get(
        f"/api/v1/exports/funds/{fund}/nav-series?range=ytd&end=2026-09-01"
    )
    assert response.status_code == 200
    assert response.headers["x-drawdown-status"] == "incomplete"
    assert response.headers["x-drawdown-start"] == "2026-06-01"
    rows = list(csv.reader(StringIO(response.text)))
    index = rows[0].index("区间回撤")
    assert [row[index] for row in rows[1:]] == [
        "0.000000",
        "0.000000",
        "-0.250000",
        "-0.083333",
    ]


def test_ytd_january_first_baseline_matches_export(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        fund = make_fund(
            session,
            nav=[
                (date(2025, 12, 31), "2.00"),
                (date(2026, 1, 1), "1.20"),
                (date(2026, 1, 6), "1.08"),
            ],
        )
    row = admin_client.get("/api/v1/dashboard/fund-drawdowns?range=ytd").json()["data"][
        "funds"
    ][0]
    assert row["status"] == "ok"
    assert row["start"] == "2026-01-01"
    assert Decimal(row["max_drawdown"]) == Decimal("-0.1")
    response = admin_client.get(
        f"/api/v1/exports/funds/{fund}/nav-series?range=ytd&end=2026-01-06"
    )
    rows = list(csv.reader(StringIO(response.text)))
    assert [row[0] for row in rows[1:]] == ["2026-01-01", "2026-01-06"]
    assert rows[-1][rows[0].index("区间回撤")] == "-0.100000"


def test_fund_drawdowns_honour_a_pinned_valuation_day(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        make_fund(session)

    pinned = admin_client.get(
        "/api/v1/dashboard/fund-drawdowns?range=all&as_of=2026-08-01"
    ).json()["data"]
    assert len(pinned["funds"]) == 1
    row = pinned["funds"][0]
    assert row["end"] == "2026-08-01"
    assert Decimal(row["current_drawdown"]) == Decimal("-0.25")
    assert Decimal(row["max_drawdown"]) == Decimal("-0.25")

    missing = admin_client.get(
        "/api/v1/dashboard/fund-drawdowns?range=all&as_of=2026-08-02"
    ).json()["data"]
    assert missing["funds"] == []


def test_fund_drawdowns_are_unavailable_without_history(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        make_fund(session, name="短历史产品", nav=[(date(2026, 9, 1), "1.00")])

    payload = admin_client.get("/api/v1/dashboard/fund-drawdowns?range=1y").json()[
        "data"
    ]
    row = next(item for item in payload["funds"] if item["name"] == "短历史产品")
    assert row["current_drawdown"] is None
    assert row["max_drawdown"] is None
    assert row["reason"] is not None and "历史不足" in row["reason"]


def test_inception_drawdown_uses_the_complete_path(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        fund = make_fund(session)

    detail = admin_client.get(f"/api/v1/funds/{fund}").json()["data"]
    inception = detail["inception_drawdown"]
    assert inception["status"] == "ok"
    assert inception["start"] == "2026-06-01"
    assert inception["end"] == "2026-09-01"
    assert Decimal(inception["max_drawdown"]) == Decimal("-0.25")
    assert Decimal(inception["current_drawdown"]).quantize(
        Decimal("0.000001")
    ) == Decimal("-0.083333")


def test_inception_drawdown_is_unavailable_when_history_is_incomplete(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        late = make_fund(
            session, name="晚起始产品", establishment_date=date(2020, 1, 1)
        )
        unknown = make_fund(session, name="无成立日产品", establishment_date=None)

    late_inception = admin_client.get(f"/api/v1/funds/{late}").json()["data"][
        "inception_drawdown"
    ]
    assert late_inception["status"] == "insufficient"
    assert late_inception["max_drawdown"] is None
    assert "历史不完整" in late_inception["reason"]

    unknown_inception = admin_client.get(f"/api/v1/funds/{unknown}").json()["data"][
        "inception_drawdown"
    ]
    assert unknown_inception["status"] == "insufficient"
    assert "成立日期" in unknown_inception["reason"]


def test_nav_series_export_carries_the_interval_drawdown(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        fund = make_fund(session)

    response = admin_client.get(f"/api/v1/exports/funds/{fund}/nav-series?range=1m")
    assert response.status_code == 200
    assert response.headers["x-drawdown-caliber"] == "standard_running_peak_in_range"
    assert response.headers["x-drawdown-range"] == "1m"
    rows = list(csv.reader(StringIO(response.text)))
    header = rows[0]
    assert "区间回撤" in header
    index = header.index("区间回撤")
    # The one-month window keeps only the 2026-08-01 baseline and 2026-09-01.
    assert [row[0] for row in rows[1:]] == ["2026-08-01", "2026-09-01"]
    assert [row[index] for row in rows[1:]] == ["0.000000", "0.000000"]

    full = admin_client.get(f"/api/v1/exports/funds/{fund}/nav-series?range=all")
    rows = list(csv.reader(StringIO(full.text)))
    index = rows[0].index("区间回撤")
    values = [row[index] for row in rows[1:]]
    assert values == ["0.000000", "0.000000", "-0.250000", "-0.083333"]


def test_overview_export_carries_the_interval_drawdown(
    admin_client, app_and_engine
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        make_fund(session)

    response = admin_client.get("/api/v1/exports/overview?range=all")
    assert response.status_code == 200
    rows = list(csv.reader(StringIO(response.text)))
    header = rows[0]
    assert "区间当前回撤" in header and "区间最大回撤" in header
    assert rows[1][header.index("区间最大回撤")] == "-0.250000"
    assert rows[1][header.index("区间起点")] == "2026-06-01"
