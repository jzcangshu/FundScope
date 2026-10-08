from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import AnalysisRunStatus, AuditResult, RiskEventStatus, RiskSeverity
from app.db.models import (
    AnalysisRun,
    AuditLog,
    BackgroundJob,
    Fund,
    RiskEvent,
    RiskRule,
    SystemState,
)

from .conftest import seed_published_fund


def _create_user(admin_client: TestClient, username: str, role: str) -> TestClient:
    response = admin_client.post(
        "/api/v1/users",
        json={"username": username, "password": "correct horse", "role": role},
    )
    assert response.status_code == 201
    client = TestClient(admin_client.app)
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct horse"},
    )
    assert login.status_code == 200
    return client


def _seed_event(engine: object) -> int:
    with Session(engine) as session:
        fund = Fund(standard_name="风险测试产品")
        rule = RiskRule(
            rule_code="drawdown_limit",
            rule_type="max_drawdown",
            scope="all",
            threshold=Decimal("-0.10"),
            severity=RiskSeverity.CRITICAL,
            version="1",
            enabled=True,
        )
        session.add_all([fund, rule])
        session.flush()
        event = RiskEvent(
            risk_rule_id=rule.id,
            fund_id=fund.id,
            valuation_date=date(2026, 8, 25),
            severity=RiskSeverity.CRITICAL,
            status=RiskEventStatus.OPEN,
            first_triggered_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
            last_triggered_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
            evidence_snapshot="drawdown=-0.12",
        )
        session.add(event)
        session.commit()
        return event.id


def test_risk_rule_patch_creates_new_version_and_keeps_history(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    created = admin_client.post(
        "/api/v1/risk/rules",
        json={
            "rule_code": "daily_loss",
            "rule_type": "daily_return",
            "scope": "all",
            "threshold": -0.05,
            "severity": "warning",
        },
    )
    assert created.status_code == 201
    first_id = created.json()["data"]["id"]
    assert created.json()["data"]["version"] == "1"

    patched = admin_client.patch(
        f"/api/v1/risk/rules/{first_id}",
        json={"threshold": -0.08, "enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["version"] == "2"
    assert patched.json()["data"]["enabled"] is False

    history = admin_client.get(
        "/api/v1/risk/rules",
        params={"include_history": "true", "rule_code": "daily_loss"},
    )
    current = admin_client.get("/api/v1/risk/rules", params={"rule_code": "daily_loss"})
    assert history.json()["meta"]["total"] == 2
    assert current.json()["meta"]["total"] == 1
    assert current.json()["data"][0]["version"] == "2"

    with Session(app_and_engine[1]) as session:
        old = session.get(RiskRule, first_id)
        assert old is not None
        assert old.version == "1"
        assert old.threshold == Decimal("-0.0500000000")
        actions = session.scalars(
            select(AuditLog.action).where(AuditLog.resource_type == "risk_rule")
        ).all()
        assert actions.count("risk_rule.version_created") == 2


def test_risk_rule_recalculate_queues_full_history_runs(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    _, engine = app_and_engine
    fund_id, _ = seed_published_fund(engine)

    response = admin_client.post("/api/v1/risk/rules/recalculate")

    assert response.status_code == 202
    payload = response.json()["data"]
    assert payload["queued_count"] == 1
    assert payload["runs"][0]["fund_id"] == fund_id
    assert payload["runs"][0]["start_date"] == "2026-08-25"
    with Session(engine) as session:
        run = session.scalar(
            select(AnalysisRun).where(
                AnalysisRun.trigger_reason == "risk_rule_recalculate"
            )
        )
        assert run is not None
        assert run.status == AnalysisRunStatus.QUEUED
        assert run.input_start_date == date(2026, 8, 25)
        job = session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "process_analysis_run",
                BackgroundJob.resource_id == str(run.id),
            )
        )
        assert job is not None

    again = admin_client.post("/api/v1/risk/rules/recalculate")
    assert again.status_code == 409

    viewer = _create_user(admin_client, "viewer-recalc", "viewer")
    assert viewer.post("/api/v1/risk/rules/recalculate").status_code == 403


def test_risk_write_payloads_reject_unknown_fields(admin_client: TestClient) -> None:
    rule = admin_client.post(
        "/api/v1/risk/rules",
        json={
            "rule_code": "strict_rule",
            "rule_type": "daily_return",
            "threshold": -0.05,
            "unexpected": "must be rejected",
        },
    )

    assert rule.status_code == 422


def test_risk_rules_and_events_are_role_scoped(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    viewer = _create_user(admin_client, "viewer-risk", "viewer")
    operator = _create_user(admin_client, "operator-risk", "operator")
    event_id = _seed_event(app_and_engine[1])

    assert viewer.get("/api/v1/risk/rules").status_code == 200
    assert viewer.get("/api/v1/risk/events").status_code == 200
    created_by_viewer = viewer.post(
        "/api/v1/risk/rules",
        json={
            "rule_code": "viewer_rule",
            "rule_type": "daily_return",
            "threshold": -0.1,
        },
    )
    assert created_by_viewer.status_code == 201

    handled = operator.post(
        f"/api/v1/risk/events/{event_id}/handle",
        json={
            "status": "acknowledged",
            "handling_note": "已通知业务员复核",
            "evidence_reference": "review/2026-08-25",
        },
    )
    assert handled.status_code == 200
    assert handled.json()["data"]["status"] == "acknowledged"

    viewer_handle = viewer.post(
        f"/api/v1/risk/events/{event_id}/handle",
        json={"status": "resolved", "handling_note": "不应允许"},
    )
    assert viewer_handle.status_code == 403

    filtered = viewer.get(
        "/api/v1/risk/events",
        params={
            "fund_id": handled.json()["data"]["fund_id"],
            "rule_code": "drawdown_limit",
            "severity": "critical",
            "status": "acknowledged",
            "start": "2026-08-25",
            "end": "2026-08-25",
        },
    )
    assert filtered.status_code == 200
    assert filtered.json()["meta"]["total"] == 1

    with Session(app_and_engine[1]) as session:
        event = session.get(RiskEvent, event_id)
        assert event is not None
        assert event.handled_by_user_id is not None
        assert event.handled_at is not None
        assert event.evidence_reference == "review/2026-08-25"
        audit = session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "risk_event.acknowledged")
            .order_by(AuditLog.id.desc())
        )
        assert audit is not None
        assert audit.actor_user_id == event.handled_by_user_id

    assert (
        operator.post(
            f"/api/v1/risk/events/{event_id}/resolve",
            json={"status": "open", "handling_note": "非法状态"},
        ).status_code
        == 422
    )
    assert (
        operator.post(
            "/api/v1/risk/events/99999/resolve",
            json={"status": "resolved", "handling_note": "不存在"},
        ).status_code
        == 404
    )


def test_system_settings_are_whitelisted_persisted_and_admin_only(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    operator = _create_user(admin_client, "operator-settings", "operator")
    viewer = _create_user(admin_client, "viewer-settings", "viewer")

    initial = admin_client.get("/api/v1/system/settings")
    assert initial.status_code == 200
    assert initial.json()["data"]["timezone"]["value"] == "Asia/Shanghai"
    assert "retention settings are read" in initial.json()["meta"]["runtime_note"]

    updated = admin_client.patch(
        "/api/v1/system/settings",
        json={
            "source_retention_days": 180,
            "backup_retention_days": 60,
            "timezone": "UTC",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["source_retention_days"] == {
        "value": 180,
        "source": "database",
    }
    assert updated.json()["data"]["timezone"] == {
        "value": "UTC",
        "source": "database",
    }

    with Session(app_and_engine[1]) as session:
        state = session.get(SystemState, 1)
        assert state is not None
        assert state.settings == {
            "source_retention_days": 180,
            "backup_retention_days": 60,
            "timezone": "UTC",
        }

    assert (
        admin_client.patch(
            "/api/v1/system/settings", json={"database_url": "sqlite:///secret.db"}
        ).status_code
        == 422
    )
    assert (
        admin_client.patch(
            "/api/v1/system/settings", json={"source_retention_days": 0}
        ).status_code
        == 422
    )
    assert (
        admin_client.patch(
            "/api/v1/system/settings", json={"timezone": "not/a-timezone"}
        ).status_code
        == 422
    )
    assert operator.get("/api/v1/system/settings").status_code == 403
    assert viewer.get("/api/v1/system/settings").status_code == 403


def test_audit_query_is_read_only_filtered_and_redacts_sensitive_summary(
    admin_client: TestClient, app_and_engine: tuple[object, object]
) -> None:
    operator = _create_user(admin_client, "operator-audit", "operator")
    viewer = _create_user(admin_client, "viewer-audit", "viewer")
    with Session(app_and_engine[1]) as session:
        session.add(
            AuditLog(
                actor_user_id=1,
                action="test.sensitive",
                resource_type="test",
                resource_id="1",
                summary={
                    "safe": "visible",
                    "password_hash": "hash-secret",
                    "token": "token-secret",
                    "mail_password": "mail-secret",
                    "database_url": "postgresql://secret",
                },
                reason="ordinary reason",
                result=AuditResult.SUCCESS,
            )
        )
        session.commit()

    response = admin_client.get(
        "/api/v1/audit-logs",
        params={"action": "test.sensitive", "result": "success", "page_size": 10},
    )
    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 1
    body = response.text
    assert "visible" in body
    assert "hash-secret" not in body
    assert "token-secret" not in body
    assert "mail-secret" not in body
    assert "postgresql://secret" not in body
    assert operator.get("/api/v1/audit-logs").status_code == 200
    assert viewer.get("/api/v1/audit-logs").status_code == 403
    assert admin_client.delete("/api/v1/audit-logs/1").status_code == 404


def test_audit_query_rejects_timezone_less_datetime(admin_client: TestClient) -> None:
    response = admin_client.get(
        "/api/v1/audit-logs", params={"start": "2026-08-27T00:00:00"}
    )

    assert response.status_code == 422


def test_mail_settings_accepts_only_the_non_sensitive_username(
    admin_client: TestClient,
) -> None:
    response = admin_client.put(
        "/api/v1/mail/settings",
        json={"host": "imap.example.test", "password": "secret"},
    )
    assert response.status_code == 422
    assert "secret" not in response.text


def test_retention_preview_and_confirmed_execution_are_admin_only(
    admin_client: TestClient,
    app_and_engine: tuple[object, object],
) -> None:
    operator = _create_user(admin_client, "operator-retention", "operator")

    preview = admin_client.post("/api/v1/system/retention/preview")
    missing_confirmation = admin_client.post(
        "/api/v1/system/retention/execute",
        json={"confirmation": "wrong", "reason": "执行到期文件清理"},
    )
    missing_reason = admin_client.post(
        "/api/v1/system/retention/execute",
        json={"confirmation": "DELETE_EXPIRED_SOURCE_FILES", "reason": "   "},
    )
    executed = admin_client.post(
        "/api/v1/system/retention/execute",
        json={
            "confirmation": "DELETE_EXPIRED_SOURCE_FILES",
            "reason": "执行到期文件清理",
        },
    )

    assert preview.status_code == 200
    assert preview.json()["data"]["summary"]["dry_run"] is True
    assert missing_confirmation.status_code == 422
    assert missing_reason.status_code == 422
    assert executed.status_code == 200
    assert executed.json()["data"]["summary"]["dry_run"] is False
    assert operator.post("/api/v1/system/retention/preview").status_code == 403
    assert (
        operator.post(
            "/api/v1/system/retention/execute",
            json={
                "confirmation": "DELETE_EXPIRED_SOURCE_FILES",
                "reason": "越权清理",
            },
        ).status_code
        == 403
    )

    with Session(app_and_engine[1]) as session:
        audit = session.scalar(
            select(AuditLog)
            .where(AuditLog.action == "system.maintenance")
            .order_by(AuditLog.id.desc())
        )
        assert audit is not None
        assert audit.actor_user_id is not None
        assert audit.reason == "执行到期文件清理"


def test_system_health_is_non_sensitive_and_admin_only(
    admin_client: TestClient,
) -> None:
    viewer = _create_user(admin_client, "viewer-health", "viewer")

    response = admin_client.get("/api/v1/system/health")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "ok",
        "database": "ok",
        "service": "fund-dashboard-api",
    }
    assert viewer.get("/api/v1/system/health").status_code == 403
