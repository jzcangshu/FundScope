from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_db
from app.config import get_settings
from app.db.base import Base
from app.db.models import AuditLog, BackgroundJob
from app.db.session import create_engine
from app.system.health import operational_summary, record_worker_heartbeat


def _create_app():
    from app.main import create_app

    return create_app()


def test_health_live_returns_stable_public_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_SERVICE_NAME", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)
    client = TestClient(_create_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "fund-dashboard-api",
    }


def test_default_health_is_not_distorted_by_external_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_SERVICE_NAME", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)

    response = TestClient(_create_app()).get("/health/live")

    assert response.json()["service"] == "fund-dashboard-api"


def test_health_uses_external_service_name_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_SERVICE_NAME", "review-env-override")
    monkeypatch.delenv("APP_PORT", raising=False)

    response = TestClient(_create_app()).get("/health/live")

    assert response.json()["service"] == "review-env-override"


def test_invalid_app_port_has_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PORT", "not-a-port")

    with pytest.raises(
        ValueError,
        match="APP_PORT must be an integer between 1 and 65535",
    ):
        get_settings()


def test_production_requires_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///unsafe.db")
    monkeypatch.setenv("UPLOAD_TEMP_DIR", "F:/fund-data/tmp")
    monkeypatch.setenv("SOURCE_STORAGE_DIR", "F:/fund-data/source")

    with pytest.raises(
        ValueError,
        match="DATABASE_URL must use PostgreSQL",
    ):
        get_settings()


def test_upload_limit_cannot_be_raised_above_20_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", str(21 * 1024 * 1024))

    with pytest.raises(ValueError, match="cannot exceed 20 MiB"):
        get_settings()


def test_operational_summary_contains_heartbeat_queue_backup_and_disk_usage(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(
        get_settings(),
        source_storage_dir=str(tmp_path),
        database_backup_dir=str(tmp_path),
        upload_temp_dir=str(tmp_path),
    )
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    try:
        with Session(engine) as session:
            record_worker_heartbeat(session, worker_id="worker-test", now=now)
            session.add(BackgroundJob(job_type="process_import_batch", resource_id="1"))
            session.add(
                AuditLog(
                    action="system.database_backup",
                    resource_type="database",
                    summary={
                        "status": "succeeded",
                        "backup_name": "database-test.dump",
                        "size_bytes": 12,
                        "command": ["hidden-command"],
                    },
                    result="success",
                )
            )
            session.commit()

            summary = operational_summary(session, settings, now=now)

        assert summary["worker"]["status"] == "healthy"
        assert summary["worker"]["worker_id"] == "worker-test"
        assert summary["queue"]["backlog"] == 1
        assert summary["backup"]["status"] == "succeeded"
        assert summary["backup"]["backup_name"] == "database-test.dump"
        assert summary["disk"]["source_storage"]["total_bytes"] > 0
        assert "hidden-command" not in str(summary)
        assert "command" not in str(summary)
    finally:
        engine.dispose()


def test_operations_endpoint_requires_operator_and_returns_summary(
    tmp_path: Path,
) -> None:
    from app.main import create_app

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app()
    app.state.settings = replace(
        app.state.settings,
        source_storage_dir=str(tmp_path),
        database_backup_dir=str(tmp_path),
        upload_temp_dir=str(tmp_path),
    )

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            assert (
                client.post(
                    "/api/v1/auth/initialize",
                    json={"username": "admin", "password": "correct horse"},
                ).status_code
                == 201
            )
            response = client.get("/api/v1/system/operations")
            assert response.status_code == 200
            assert "database" in response.json()["data"]
            assert "token" not in response.text.casefold()
            assert "password" not in response.text.casefold()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_cors_allows_tauri_desktop_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Tauri desktop client runs under tauri.localhost and needs CORS headers."""

    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("APP_SERVICE_NAME", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)

    client = TestClient(_create_app())

    preflight = client.options(
        "/api/v1/auth/status",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert "GET" in preflight.headers["access-control-allow-methods"]


def test_cors_rejects_unlisted_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Origins not in the allowlist must not receive CORS headers."""

    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("APP_SERVICE_NAME", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)

    client = TestClient(_create_app())

    response = client.get(
        "/health/live",
        headers={"Origin": "http://evil.example.com"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_origins_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """CORS_ORIGINS env var replaces the default allowlist."""

    monkeypatch.setenv("CORS_ORIGINS", "https://custom.example.com")
    monkeypatch.delenv("APP_SERVICE_NAME", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)

    client = TestClient(_create_app())

    preflight = client.options(
        "/health/live",
        headers={
            "Origin": "https://custom.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 200
    assert (
        preflight.headers["access-control-allow-origin"] == "https://custom.example.com"
    )

    rejected = client.options(
        "/health/live",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
        },
    )
    # tauri.localhost is not in the override, so it should not be allowed.
    assert (
        rejected.headers.get("access-control-allow-origin") != "http://tauri.localhost"
    )
