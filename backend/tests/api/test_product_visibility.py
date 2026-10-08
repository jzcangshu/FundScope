from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.base import FundStatus, RiskSeverity, ValidationLevel, ValuationStatus
from app.db.models import (
    FundDailySnapshot,
    RiskEvent,
    RiskRule,
    ValidationResult,
    ValuationVersion,
)
from app.publishing import PublishingService

from .conftest import seed_published_fund


def restricted_client(admin_client, app, ids, role="viewer"):
    response = admin_client.post(
        "/api/v1/users",
        json={
            "username": "scoped",
            "password": "test-password",
            "role": role,
            "fund_access_mode": "selected",
            "fund_ids": ids,
        },
    )
    assert response.status_code == 201, response.text
    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login",
        json={
            "username": "scoped",
            "password": "test-password",
        },
    )
    assert login.status_code == 200
    return client, response.json()["data"]["id"]


@pytest.mark.parametrize("role", ["viewer", "operator", "boss"])
def test_lists_totals_and_exports_use_only_grants(admin_client, app_and_engine, role):
    app, engine = app_and_engine
    first, _ = seed_published_fund(engine, name="allowed")
    hidden, _ = seed_published_fund(engine, name="private")
    inactive, _ = seed_published_fund(
        engine, name="inactive", fund_status=FundStatus.INACTIVE
    )
    client, _ = restricted_client(admin_client, app, [first, inactive], role)
    with client:
        page = client.get("/api/v1/funds?page_size=1").json()
        assert page["meta"]["total"] == 2
        assert page["data"][0]["id"] == first
        assert client.get("/api/v1/funds?q=private").json()["meta"]["total"] == 0
        overview = client.get("/api/v1/dashboard/overview").json()
        assert overview["data"]["fund_count"] == 1
        assert Decimal(overview["data"]["total_net_assets"]) == Decimal(90000)
        assert overview["meta"]["coverage"] == {"available": 1, "total": 1}
        assert [fund["id"] for fund in overview["data"]["funds"]] == [first]
        draws = client.get("/api/v1/dashboard/fund-drawdowns").json()
        assert [fund["fund_id"] for fund in draws["data"]["funds"]] == [first]
        for endpoint in ("funds", "overview?range=1m"):
            response = client.get(f"/api/v1/exports/{endpoint}")
            assert response.status_code == 200
            assert "allowed" in response.text
            assert "private" not in response.text and "inactive" not in response.text
            assert response.headers["cache-control"] == "no-store, private"
        for endpoint in ("", "/nav-series", "/positions", "/quality"):
            response = client.get(f"/api/v1/funds/{hidden}{endpoint}")
            assert response.status_code == 404, response.text
        for endpoint in (
            "overview",
            "nav-series",
            "positions",
            "allocation",
            "share-classes",
        ):
            assert (
                client.get(f"/api/v1/exports/funds/{hidden}/{endpoint}").status_code
                == 404
            )
            assert (
                client.get(f"/api/v1/exports/funds/{first}/{endpoint}").status_code
                == 200
            )


def test_series_recalculates_authorized_portfolio_and_ignores_hidden_dates(
    admin_client, app_and_engine
):
    app, engine = app_and_engine
    first, _ = seed_published_fund(engine, name="allowed", unit_nav=Decimal(1))
    seed_published_fund(
        engine, name="private", valuation_date=date(2026, 9, 10), unit_nav=Decimal(99)
    )
    with Session(engine) as session:
        version = ValuationVersion(
            fund_id=first,
            valuation_date=date(2026, 8, 26),
            version_no=1,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(version)
        session.flush()
        session.add(
            FundDailySnapshot(
                valuation_version_id=version.id,
                unit_nav=Decimal("1.1"),
                cumulative_unit_nav=Decimal("1.1"),
                net_asset_value=Decimal(99000),
            )
        )
        session.flush()
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="test",
                level=ValidationLevel.INFO,
                message="valid",
            )
        )
        session.flush()
        PublishingService(session).publish_version(
            version.id, actor_user_id=1, actor_label="admin", reason="test"
        )
        session.commit()
    client, _ = restricted_client(admin_client, app, [first])
    with client:
        response = client.get("/api/v1/dashboard/series")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["meta"]["end"] == "2026-08-26"
        points = payload["data"]["points"]
        assert [p["valuation_date"] for p in points] == ["2026-08-25", "2026-08-26"]
        assert Decimal(points[-1]["company_index"]) == Decimal("1.1")
        assert Decimal(points[-1]["company_daily_return"]) == Decimal("0.1")
        assert Decimal(points[-1]["total_assets"]) == Decimal(99000)
        assert points[-1]["effective_fund_count"] == 1
        overview = client.get("/api/v1/dashboard/overview").json()["data"]
        assert Decimal(overview["company_index"]) == Decimal("1.1")
        narrowed = client.get(
            "/api/v1/dashboard/series?start=2026-08-26&end=2026-08-26"
        ).json()
        assert Decimal(narrowed["data"]["points"][0]["company_index"]) == Decimal("1.1")
        assert (
            client.get(
                "/api/v1/dashboard/series?start=2026-08-27&end=2026-08-26"
            ).status_code
            == 422
        )


def test_revoking_all_grants_applies_to_existing_session(admin_client, app_and_engine):
    app, engine = app_and_engine
    first, _ = seed_published_fund(engine)
    client, user_id = restricted_client(admin_client, app, [first])
    with client:
        assert client.get(f"/api/v1/funds/{first}").status_code == 200
        updated = admin_client.patch(
            f"/api/v1/users/{user_id}/fund-access",
            json={
                "fund_access_mode": "selected",
                "fund_ids": [],
                "reason": "revoke product access",
            },
        )
        assert updated.status_code == 200, updated.text
        assert client.get(f"/api/v1/funds/{first}").status_code == 404
        assert client.get("/api/v1/funds").json()["data"] == []
        assert client.get("/api/v1/dashboard/series").json()["data"]["points"] == []
        assert (
            client.get("/api/v1/dashboard/fund-drawdowns").json()["data"]["funds"] == []
        )
        overview = client.get("/api/v1/dashboard/overview").json()
        assert overview["data"]["fund_count"] == 0
        assert overview["data"]["company_index"] is None
        assert overview["meta"]["coverage"]["total"] == 0


def test_risk_filters_counts_and_write_authorization(admin_client, app_and_engine):
    app, engine = app_and_engine
    first, _ = seed_published_fund(engine, name="allowed")
    hidden, _ = seed_published_fund(engine, name="private")
    with Session(engine) as session:
        rule = RiskRule(
            rule_code="test",
            rule_type="drawdown",
            scope="fund",
            severity=RiskSeverity.WARNING,
            version="1",
        )
        session.add(rule)
        session.flush()
        events = [
            RiskEvent(
                risk_rule_id=rule.id,
                fund_id=fund_id,
                valuation_date=date(2026, 8, 25),
                severity=RiskSeverity.WARNING,
                first_triggered_at=datetime.now(UTC),
                last_triggered_at=datetime.now(UTC),
            )
            for fund_id in (first, hidden, None)
        ]
        session.add_all(events)
        session.commit()
        event_ids = [event.id for event in events]
    client, _ = restricted_client(admin_client, app, [first], "operator")
    with client:
        response = client.get("/api/v1/risk/events")
        assert response.json()["meta"]["total"] == 1
        assert [event["id"] for event in response.json()["data"]] == [event_ids[0]]
        assert (
            client.get("/api/v1/dashboard/overview").json()["data"]["risk_event_count"]
            == 1
        )
        assert client.get(f"/api/v1/risk/events?fund_id={hidden}").status_code == 404
        for event_id in event_ids[1:]:
            assert (
                client.post(
                    f"/api/v1/risk/events/{event_id}/handle",
                    json={"status": "resolved", "handling_note": "reviewed"},
                ).status_code
                == 404
            )
        assert (
            client.post(
                f"/api/v1/risk/events/{event_ids[0]}/handle",
                json={"status": "resolved", "handling_note": "reviewed"},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/risk/rules").status_code == 403
        assert client.post("/api/v1/risk/rules/recalculate").status_code == 403
