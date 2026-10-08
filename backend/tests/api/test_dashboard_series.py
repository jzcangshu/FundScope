from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.analytics.scope import COMPANY_SCOPE_TRIGGER
from app.db.base import AnalysisRunStatus, FundStatus, ValuationStatus
from app.db.models import (
    AnalysisRun,
    CompanyMetricDaily,
    Fund,
    FundDailySnapshot,
    ValuationVersion,
)

from .conftest import seed_published_fund

LATEST = date(2026, 8, 25)


def _succeed_published_run(engine: object, version_id: int) -> int:
    with Session(engine) as session:  # type: ignore[arg-type]
        run = session.scalar(
            select(AnalysisRun).where(AnalysisRun.trigger_version_id == version_id)
        )
        assert run is not None
        run.status = AnalysisRunStatus.SUCCEEDED
        session.commit()
        return run.id


def _add_company_metric(
    engine: object,
    *,
    run_id: int,
    valuation_date: date = LATEST,
    company_index: Decimal = Decimal("1.10"),
) -> None:
    with Session(engine) as session:  # type: ignore[arg-type]
        session.add(
            CompanyMetricDaily(
                valuation_date=valuation_date,
                source_analysis_run_id=run_id,
                company_index=company_index,
                company_daily_return=Decimal("0.01"),
                effective_fund_count=1,
                total_net_assets=Decimal(90000),
            )
        )
        session.commit()


def _add_scope_run(engine: object, *, status: AnalysisRunStatus) -> None:
    with Session(engine) as session:  # type: ignore[arg-type]
        session.add(
            AnalysisRun(
                trigger_reason=COMPANY_SCOPE_TRIGGER,
                methodology_version="v1",
                status=status,
            )
        )
        session.commit()


def _seed_company_data(engine: object) -> int:
    _fund_id, version_id = seed_published_fund(engine)
    run_id = _succeed_published_run(engine, version_id)
    _add_company_metric(engine, run_id=run_id)
    return run_id


def test_series_returns_points_with_ready_status(admin_client, app_and_engine) -> None:
    _seed_company_data(app_and_engine[1])

    response = admin_client.get("/api/v1/dashboard/series")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["methodology"] == "daily_nav_weighted_index"
    assert body["meta"]["analysis_status"] == "ready"
    assert body["meta"]["coverage"] == {"available": 1, "total": 1}
    assert body["meta"]["end"] == LATEST.isoformat()
    # Default window is one year back from the latest published date.
    assert body["meta"]["start"] == (LATEST - timedelta(days=365)).isoformat()
    assert body["meta"]["max_drawdown"] is not None
    point = body["data"]["points"][0]
    assert point["valuation_date"] == LATEST.isoformat()
    assert point["company_index"] == "1.1000000000"
    assert point["company_daily_return"] == "0.0100000000"
    assert point["total_net_assets"] == "90000.0000000000"
    assert point["total_assets"] == "90000.0000000000"
    assert point["effective_fund_count"] == 1
    # A single point sits at its own peak, so drawdown is zero.
    assert float(point["drawdown"]) == pytest.approx(0.0)


def test_series_computes_drawdown_across_points(admin_client, app_and_engine) -> None:
    engine = app_and_engine[1]
    _fund_id, version_id = seed_published_fund(engine)
    # Give the previous day a published valuation too, otherwise the
    # published-date filter would drop it from the series.
    with Session(engine) as session:
        session.add(
            ValuationVersion(
                fund_id=_fund_id,
                valuation_date=LATEST - timedelta(days=1),
                version_no=1,
                status=ValuationStatus.PUBLISHED,
            )
        )
        session.commit()
    run_id = _succeed_published_run(engine, version_id)
    _add_company_metric(
        engine,
        run_id=run_id,
        valuation_date=LATEST - timedelta(days=1),
        company_index=Decimal("1.10"),
    )
    _add_company_metric(
        engine,
        run_id=run_id,
        valuation_date=LATEST,
        company_index=Decimal("1.00"),
    )

    body = admin_client.get("/api/v1/dashboard/series").json()

    trough = body["data"]["points"][-1]
    assert float(trough["drawdown"]) == pytest.approx(-1 / 11, abs=1e-8)
    assert float(body["meta"]["max_drawdown"]) == pytest.approx(-1 / 11, abs=1e-8)
    assert float(body["meta"]["current_drawdown"]) == pytest.approx(-1 / 11, abs=1e-8)


def test_series_rejects_oversized_and_inverted_windows(
    admin_client, app_and_engine
) -> None:
    _seed_company_data(app_and_engine[1])
    url = "/api/v1/dashboard/series"

    oversized_both = admin_client.get(
        url, params={"start": "2020-01-01", "end": LATEST.isoformat()}
    )
    oversized_start_only = admin_client.get(url, params={"start": "2020-01-01"})
    inverted = admin_client.get(
        url, params={"start": LATEST.isoformat(), "end": "2026-08-24"}
    )
    # Exactly five years (365*5 days) is the largest allowed span.
    exact_limit = admin_client.get(
        url,
        params={"start": (LATEST - timedelta(days=365 * 5)).isoformat()},
    )

    assert oversized_both.status_code == 422
    assert oversized_start_only.status_code == 422
    assert inverted.status_code == 422
    assert exact_limit.status_code == 200


def test_series_scope_run_not_succeeded_hides_data(
    admin_client, app_and_engine
) -> None:
    _seed_company_data(app_and_engine[1])

    _add_scope_run(app_and_engine[1], status=AnalysisRunStatus.QUEUED)
    pending = admin_client.get("/api/v1/dashboard/series").json()

    with Session(app_and_engine[1]) as session:
        scope_run = session.scalar(
            select(AnalysisRun).where(
                AnalysisRun.trigger_reason == COMPANY_SCOPE_TRIGGER
            )
        )
        assert scope_run is not None
        scope_run.status = AnalysisRunStatus.FAILED
        session.commit()

    stale = admin_client.get("/api/v1/dashboard/series").json()

    assert pending["meta"]["analysis_status"] == "pending"
    assert pending["data"]["points"] == []
    assert stale["meta"]["analysis_status"] == "stale"
    assert stale["data"]["points"] == []


def test_series_excludes_runs_older_than_company_scope(
    admin_client, app_and_engine
) -> None:
    _seed_company_data(app_and_engine[1])
    _add_scope_run(app_and_engine[1], status=AnalysisRunStatus.SUCCEEDED)

    body = admin_client.get("/api/v1/dashboard/series").json()

    # The published fund's analysis run predates the company scope run, so its
    # metrics are stale for the current company scope.
    assert body["meta"]["analysis_status"] == "ready"
    assert body["data"]["points"] == []


def test_series_prefers_latest_run_per_date(admin_client, app_and_engine) -> None:
    engine = app_and_engine[1]
    _fund_id, version_id = seed_published_fund(engine)
    original_run_id = _succeed_published_run(engine, version_id)
    _add_company_metric(engine, run_id=original_run_id, company_index=Decimal("1.10"))

    with Session(engine) as session:
        recalculation = AnalysisRun(
            trigger_version_id=version_id,
            trigger_reason="historical_revision",
            input_start_date=LATEST,
            input_end_date=LATEST,
            methodology_version="v1",
            status=AnalysisRunStatus.SUCCEEDED,
        )
        session.add(recalculation)
        session.commit()
        recalculation_id = recalculation.id

    _add_company_metric(engine, run_id=recalculation_id, company_index=Decimal("1.20"))

    points = admin_client.get("/api/v1/dashboard/series").json()["data"]["points"]

    assert len(points) == 1
    assert points[0]["company_index"] == "1.2000000000"


def test_series_excludes_dates_without_published_valuation(
    admin_client, app_and_engine
) -> None:
    engine = app_and_engine[1]
    _fund_id, version_id = seed_published_fund(engine)
    run_id = _succeed_published_run(engine, version_id)
    _add_company_metric(
        engine,
        run_id=run_id,
        valuation_date=LATEST - timedelta(days=1),
        company_index=Decimal("1.05"),
    )
    _add_company_metric(engine, run_id=run_id)

    points = admin_client.get("/api/v1/dashboard/series").json()["data"]["points"]

    assert [point["valuation_date"] for point in points] == [LATEST.isoformat()]


def test_series_empty_database_returns_pending(admin_client) -> None:
    body = admin_client.get("/api/v1/dashboard/series").json()

    assert body["meta"]["analysis_status"] == "pending"
    assert body["data"]["points"] == []
    assert body["meta"]["coverage"] == {"available": 0, "total": 0}


def test_series_total_assets_use_same_day_published_active_products(
    admin_client, app_and_engine
) -> None:
    engine = app_and_engine[1]
    _first_fund_id, version_id = seed_published_fund(engine)
    seed_published_fund(engine, name="第二只产品")
    inactive_fund_id, _ = seed_published_fund(engine, name="停用产品")
    with Session(engine) as session:
        inactive_fund = session.get(Fund, inactive_fund_id)
        assert inactive_fund is not None
        inactive_fund.status = FundStatus.INACTIVE
        session.commit()
    run_id = _succeed_published_run(engine, version_id)
    _add_company_metric(engine, run_id=run_id)

    points = admin_client.get("/api/v1/dashboard/series").json()["data"]["points"]

    assert [Decimal(point["total_assets"]) for point in points] == [Decimal(180000)]
    assert points[0]["company_index"] == "1.1000000000"
    assert points[0]["total_net_assets"] == "90000.0000000000"
    bounded = admin_client.get(
        "/api/v1/dashboard/series", params={"start": LATEST.isoformat()}
    ).json()["data"]["points"]
    assert len(bounded) == 1
    assert bounded[0]["total_assets"] == "180000.0000000000"


@pytest.mark.parametrize("mode", ["no_values", "no_snapshot"])
def test_series_total_assets_skip_funds_without_reported_net_assets(
    admin_client, app_and_engine, mode
) -> None:
    engine = app_and_engine[1]
    _seed_company_data(engine)
    _, incomplete_version_id = seed_published_fund(engine, name="缺失净值的产品")
    with Session(engine) as session:
        snapshot = session.scalar(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id == incomplete_version_id
            )
        )
        assert snapshot is not None
        if mode == "no_snapshot":
            session.execute(
                delete(FundDailySnapshot).where(FundDailySnapshot.id == snapshot.id)
            )
        else:
            session.execute(
                update(FundDailySnapshot)
                .where(FundDailySnapshot.id == snapshot.id)
                .values(net_asset_value=None)
            )
        session.commit()

    point = admin_client.get("/api/v1/dashboard/series").json()["data"]["points"][0]

    # With no reported net assets the fund is simply not in the total yet;
    # the day still sums the funds that have reported (90000).
    assert Decimal(point["total_assets"]) == Decimal(90000)
    # The existing investment-return calculation is unaffected by assets.
    assert point["company_index"] == "1.1000000000"
    assert float(point["drawdown"]) == pytest.approx(0.0)


def test_series_total_assets_carry_each_funds_latest_published_value(
    admin_client, app_and_engine
) -> None:
    engine = app_and_engine[1]
    _first_fund_id, first_version_id = seed_published_fund(
        engine, name="隔日发布的产品", valuation_date=LATEST - timedelta(days=1)
    )
    first_run_id = _succeed_published_run(engine, first_version_id)
    _add_company_metric(
        engine, run_id=first_run_id, valuation_date=LATEST - timedelta(days=1)
    )
    _second_fund_id, second_version_id = seed_published_fund(
        engine, name="当日发布的产品"
    )
    second_run_id = _succeed_published_run(engine, second_version_id)
    _add_company_metric(engine, run_id=second_run_id)

    points = admin_client.get("/api/v1/dashboard/series").json()["data"]["points"]

    # The late publisher has no data yet on its first metric date; the next
    # day it joins while the first fund keeps its own last published figure.
    assert [Decimal(p["total_assets"]) for p in points] == [
        Decimal(90000),
        Decimal(180000),
    ]


def test_series_total_assets_preserve_a_valid_zero(
    admin_client, app_and_engine
) -> None:
    engine = app_and_engine[1]
    _seed_company_data(engine)
    with Session(engine) as session:
        snapshot = session.scalar(select(FundDailySnapshot))
        assert snapshot is not None
        session.execute(
            update(FundDailySnapshot)
            .where(FundDailySnapshot.id == snapshot.id)
            .values(net_asset_value=Decimal(0))
        )
        session.commit()

    point = admin_client.get("/api/v1/dashboard/series").json()["data"]["points"][0]

    assert Decimal(point["total_assets"]) == Decimal(0)


def test_series_requires_authentication_and_allows_all_roles(
    admin_client, app_and_engine
) -> None:
    _seed_company_data(app_and_engine[1])

    created = admin_client.post(
        "/api/v1/users",
        json={"username": "viewer", "password": "correct horse", "role": "viewer"},
    )
    assert created.status_code == 201

    viewer = TestClient(admin_client.app)
    viewer.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct horse"},
    )
    anonymous = TestClient(admin_client.app)

    assert viewer.get("/api/v1/dashboard/series").status_code == 200
    assert anonymous.get("/api/v1/dashboard/series").status_code == 401
