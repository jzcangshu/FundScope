from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.db.base import AnalysisRunStatus, FundStatus, ValuationStatus
from app.db.models import (
    AnalysisRun,
    FundMetricDaily,
    ValuationVersion,
)

from .conftest import seed_published_fund


def test_dashboard_overview_and_fund_queries_are_published_only(
    admin_client, app_and_engine
) -> None:
    fund_id, version_id = seed_published_fund(app_and_engine[1])

    overview = admin_client.get("/api/v1/dashboard/overview")
    funds = admin_client.get("/api/v1/funds", params={"page": 1, "page_size": 20})
    detail = admin_client.get(f"/api/v1/funds/{fund_id}")
    nav = admin_client.get(f"/api/v1/funds/{fund_id}/nav-series")
    positions = admin_client.get(f"/api/v1/funds/{fund_id}/positions")
    quality = admin_client.get(f"/api/v1/funds/{fund_id}/quality")

    assert overview.status_code == 200
    assert overview.json()["data"]["total_net_assets"] == "90000.0000000000"
    assert overview.json()["meta"]["coverage"] == {"available": 1, "total": 1}
    assert funds.json()["meta"]["total"] == 1
    assert funds.json()["data"][0]["id"] == fund_id
    assert detail.json()["data"]["current_version_id"] == version_id
    assert nav.json()["data"]["points"][0]["unit_nav"] == "1.2500000000"
    assert positions.json()["meta"]["total"] == 1
    assert quality.json()["data"]["validation"][0]["rule_code"] == "test_rule"


def test_dashboard_has_date_and_pagination_filters(
    admin_client, app_and_engine
) -> None:
    seed_published_fund(app_and_engine[1], name="梦溪一号")
    seed_published_fund(
        app_and_engine[1],
        name="千帆一号",
        valuation_date=date(2026, 8, 24),
        unit_nav=Decimal("1.10"),
    )

    response = admin_client.get(
        "/api/v1/funds",
        params={"q": "千帆", "as_of": "2026-08-24", "page": 1, "page_size": 1},
    )
    overview = admin_client.get(
        "/api/v1/dashboard/overview", params={"as_of": "2026-08-24"}
    )

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 1
    assert response.json()["data"][0]["name"] == "千帆一号"
    assert overview.status_code == 200
    assert overview.json()["data"]["total_net_assets"] == "90000.0000000000"


def test_dashboard_reports_failed_analysis_as_stale(
    admin_client, app_and_engine
) -> None:
    fund_id, version_id = seed_published_fund(app_and_engine[1])
    with Session(app_and_engine[1]) as session:
        run = session.scalar(
            select(AnalysisRun).where(AnalysisRun.trigger_version_id == version_id)
        )
        assert run is not None
        run.status = AnalysisRunStatus.FAILED
        session.commit()

    funds = admin_client.get("/api/v1/funds")
    detail = admin_client.get(f"/api/v1/funds/{fund_id}")
    overview = admin_client.get("/api/v1/dashboard/overview")

    assert funds.json()["data"][0]["analysis_status"] == "stale"
    assert detail.json()["data"]["analysis_status"] == "stale"
    assert overview.json()["meta"]["analysis_status"] == "stale"


def test_dashboard_prefers_latest_successful_recalculation(
    admin_client, app_and_engine
) -> None:
    fund_id, version_id = seed_published_fund(app_and_engine[1])
    with Session(app_and_engine[1]) as session:
        original_run = session.scalar(
            select(AnalysisRun).where(AnalysisRun.trigger_version_id == version_id)
        )
        assert original_run is not None
        original_run.status = AnalysisRunStatus.SUCCEEDED
        session.add(
            FundMetricDaily(
                fund_id=fund_id,
                valuation_date=date(2026, 8, 25),
                source_analysis_run_id=original_run.id,
                daily_return=Decimal("0.01"),
            )
        )
        historical_version = ValuationVersion(
            fund_id=fund_id,
            valuation_date=date(2026, 8, 24),
            version_no=1,
            status=ValuationStatus.PUBLISHED,
        )
        session.add(historical_version)
        session.flush()
        recalculation = AnalysisRun(
            trigger_version_id=historical_version.id,
            trigger_reason="historical_revision",
            input_start_date=historical_version.valuation_date,
            input_end_date=date(2026, 8, 25),
            methodology_version="v1",
            status=AnalysisRunStatus.SUCCEEDED,
        )
        session.add(recalculation)
        session.flush()
        recalculation_id = recalculation.id
        session.add(
            FundMetricDaily(
                fund_id=fund_id,
                valuation_date=date(2026, 8, 25),
                source_analysis_run_id=recalculation.id,
                daily_return=Decimal("0.02"),
            )
        )
        session.commit()

    funds = admin_client.get("/api/v1/funds")

    assert funds.json()["data"][0]["daily_return"] == "0.0200000000"
    assert funds.json()["data"][0]["analysis_run_id"] == recalculation_id


def test_nav_series_prefers_persisted_metrics_and_reports_fallback(
    admin_client, app_and_engine
) -> None:
    fund_id, version_id = seed_published_fund(app_and_engine[1])

    pending = admin_client.get(f"/api/v1/funds/{fund_id}/nav-series")
    assert pending.json()["meta"]["analysis_status"] == "pending"
    assert pending.json()["data"]["points"][0]["metric_source"] == (
        "calculated_fallback"
    )

    with Session(app_and_engine[1]) as session:
        run = session.scalar(
            select(AnalysisRun).where(AnalysisRun.trigger_version_id == version_id)
        )
        assert run is not None
        run.status = AnalysisRunStatus.SUCCEEDED
        session.add(
            FundMetricDaily(
                fund_id=fund_id,
                valuation_date=date(2026, 8, 25),
                source_analysis_run_id=run.id,
                daily_return=Decimal("0.123"),
                cumulative_return=Decimal("0.456"),
            )
        )
        session.commit()

    ready = admin_client.get(f"/api/v1/funds/{fund_id}/nav-series")
    point = ready.json()["data"]["points"][0]

    assert ready.json()["meta"]["analysis_status"] == "ready"
    assert ready.json()["meta"]["metric_source"] == "persisted"
    assert point["daily_return"] == "0.1230000000"
    assert point["cumulative_return"] == "0.4560000000"
    assert point["metric_source"] == "persisted"


def test_fund_list_reports_total_and_empty_out_of_range_page(
    admin_client, app_and_engine
) -> None:
    for name in ("甲产品", "乙产品", "丙产品"):
        seed_published_fund(app_and_engine[1], name=name)

    last_page = admin_client.get("/api/v1/funds", params={"page": 2, "page_size": 2})
    out_of_range = admin_client.get("/api/v1/funds", params={"page": 3, "page_size": 2})

    assert last_page.status_code == 200
    assert len(last_page.json()["data"]) == 1
    assert last_page.json()["meta"] == {"page": 2, "page_size": 2, "total": 3}
    assert out_of_range.status_code == 200
    assert out_of_range.json()["data"] == []
    assert out_of_range.json()["meta"] == {
        "page": 3,
        "page_size": 2,
        "total": 3,
    }


def test_fund_list_query_count_is_constant_for_page_size(
    admin_client, app_and_engine
) -> None:
    engine = app_and_engine[1]
    for index in range(8):
        seed_published_fund(engine, name=f"批量产品{index}")

    select_count = 0

    def count_selects(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        response = admin_client.get(
            "/api/v1/funds", params={"page": 1, "page_size": 20}
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert response.status_code == 200
    assert len(response.json()["data"]) == 8
    assert select_count <= 10


def test_positions_report_total_and_empty_out_of_range_page(
    admin_client, app_and_engine
) -> None:
    fund_id, _ = seed_published_fund(app_and_engine[1], position_count=3)

    last_page = admin_client.get(
        f"/api/v1/funds/{fund_id}/positions",
        params={"page": 2, "page_size": 2},
    )
    out_of_range = admin_client.get(
        f"/api/v1/funds/{fund_id}/positions",
        params={"page": 3, "page_size": 2},
    )

    assert last_page.status_code == 200
    assert len(last_page.json()["data"]) == 1
    assert last_page.json()["meta"]["total"] == 3
    assert out_of_range.status_code == 200
    assert out_of_range.json()["data"] == []
    assert out_of_range.json()["meta"]["total"] == 3


def test_dashboard_uses_exact_date_and_excludes_inactive_funds(
    admin_client, app_and_engine
) -> None:
    seed_published_fund(
        app_and_engine[1],
        name="停用产品",
        fund_status=FundStatus.INACTIVE,
    )
    seed_published_fund(
        app_and_engine[1],
        name="较晚产品",
        valuation_date=date(2026, 8, 25),
    )

    response = admin_client.get(
        "/api/v1/dashboard/overview", params={"as_of": "2026-08-24"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["fund_count"] == 0
    assert response.json()["data"]["total_net_assets"] is None
    assert response.json()["meta"]["coverage"] == {"available": 0, "total": 1}


def test_viewer_can_read_dashboard_but_cannot_operate(
    admin_client, app_and_engine
) -> None:
    seed_published_fund(app_and_engine[1])
    created = admin_client.post(
        "/api/v1/users",
        json={
            "username": "viewer",
            "password": "correct horse",
            "role": "viewer",
        },
    )
    assert created.status_code == 201

    from fastapi.testclient import TestClient

    viewer = TestClient(admin_client.app)
    viewer.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct horse"},
    )

    assert viewer.get("/api/v1/dashboard/overview").status_code == 200
    assert viewer.get("/api/v1/reviews").status_code == 403
    assert (
        viewer.post("/api/v1/imports", json={"source_type": "upload"}).status_code
        == 403
    )


def test_login_navigation_and_user_list_are_role_scoped(admin_client) -> None:
    created = admin_client.post(
        "/api/v1/users",
        json={
            "username": "operator",
            "password": "correct horse",
            "role": "operator",
        },
    )
    listed = admin_client.get(
        "/api/v1/users", params={"role": "operator", "page": 1, "page_size": 10}
    )

    assert "users" in admin_client.get("/api/v1/auth/me").json()["data"]["navigation"]
    assert created.status_code == 201
    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] == 1
    assert listed.json()["data"][0]["username"] == "operator"


def test_nav_series_caps_default_window_to_one_year(
    admin_client, app_and_engine
) -> None:
    """nav-series must default to a 365-day window so long-running funds
    do not OOM the API worker on each render.
    """
    engine = app_and_engine[1]
    # Seed two funds because within a single fund each ValuationVersion is
    # published at most once via PublishingService.
    fund_id, _ = seed_published_fund(
        engine,
        name="长期产品近",
        valuation_date=date.today() - timedelta(days=180),
        unit_nav=Decimal("1.30"),
    )
    fund_id_old, _ = seed_published_fund(
        engine,
        name="长期产品远",
        valuation_date=date.today() - timedelta(days=900),
        unit_nav=Decimal("1.05"),
    )

    # The default window is 365 days; the older fund's published
    # valuation should be excluded.
    response = admin_client.get(f"/api/v1/funds/{fund_id_old}/nav-series")
    assert response.status_code == 200
    assert response.json()["data"]["points"] == []

    # The newer fund should be in range and surface its NAV point.
    response = admin_client.get(f"/api/v1/funds/{fund_id}/nav-series")
    assert response.status_code == 200
    points = response.json()["data"]["points"]
    assert len(points) == 1
    assert (
        points[0]["valuation_date"] == (date.today() - timedelta(days=180)).isoformat()
    )

    # When the client explicitly widens the window (within the 5-year
    # guard) the older fund's point comes back too.
    response = admin_client.get(
        f"/api/v1/funds/{fund_id_old}/nav-series",
        params={
            "start": (date.today() - timedelta(days=1000)).isoformat(),
            "end": date.today().isoformat(),
        },
    )
    points = response.json()["data"]["points"]
    assert len(points) == 1


def test_nav_series_only_end_preserves_bounded_history(
    admin_client, app_and_engine
) -> None:
    """An explicit end can include older history within the five-year cap."""
    engine = app_and_engine[1]
    fund_id_old, _ = seed_published_fund(
        engine,
        name="只传截止产品",
        valuation_date=date.today() - timedelta(days=900),
        unit_nav=Decimal("1.10"),
    )
    # The 900-day-old point is still inside the bounded history window.
    response = admin_client.get(
        f"/api/v1/funds/{fund_id_old}/nav-series",
        params={"end": date.today().isoformat()},
    )
    assert response.status_code == 200
    points = response.json()["data"]["points"]
    assert len(points) == 1
    assert (
        points[0]["valuation_date"] == (date.today() - timedelta(days=900)).isoformat()
    )


def test_nav_series_rejects_window_exceeding_five_years(
    admin_client, app_and_engine
) -> None:
    """An explicit window wider than 5 years must be rejected with 422
    so a single request cannot OOM the worker.
    """
    engine = app_and_engine[1]
    fund_id, _ = seed_published_fund(
        engine,
        name="窗口超限产品",
        valuation_date=date.today() - timedelta(days=180),
        unit_nav=Decimal("1.40"),
    )
    response = admin_client.get(
        f"/api/v1/funds/{fund_id}/nav-series",
        params={
            "start": (date.today() - timedelta(days=365 * 6)).isoformat(),
            "end": date.today().isoformat(),
        },
    )
    assert response.status_code == 422


def test_nav_series_end_only_excludes_history_beyond_five_years(
    admin_client, app_and_engine
) -> None:
    fund_id, _ = seed_published_fund(
        app_and_engine[1],
        name="超出单边窗口的历史",
        valuation_date=date.today() - timedelta(days=365 * 6),
    )
    response = admin_client.get(
        f"/api/v1/funds/{fund_id}/nav-series", params={"end": date.today().isoformat()}
    )

    assert response.status_code == 200
    assert response.json()["data"]["points"] == []


def test_nav_series_start_only_cannot_bypass_span_limit(
    admin_client, app_and_engine
) -> None:
    fund_id, _ = seed_published_fund(app_and_engine[1])
    response = admin_client.get(
        f"/api/v1/funds/{fund_id}/nav-series",
        params={"start": (date.today() - timedelta(days=365 * 6)).isoformat()},
    )

    assert response.status_code == 422


def test_nav_series_rejects_inverted_range_and_handles_earliest_end(
    admin_client, app_and_engine
) -> None:
    fund_id, _ = seed_published_fund(app_and_engine[1])
    url = f"/api/v1/funds/{fund_id}/nav-series"
    inverted = admin_client.get(
        url, params={"start": "2026-09-01", "end": "2026-08-01"}
    )
    earliest = admin_client.get(url, params={"end": "0001-01-01"})

    assert inverted.status_code == 422
    assert earliest.status_code == 200
    assert earliest.json()["data"]["points"] == []
