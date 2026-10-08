from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import AuditResult, Base, JobStatus
from app.db.models import AuditLog, BackgroundJob, SystemState
from app.db.session import create_engine
from app.mail import MailConfigurationError, MailConnectionError
from app.maintenance_cli import build_parser
from app.system.maintenance import (
    MaintenanceResult,
    MaintenanceService,
    ScheduleConfig,
    summarize_jobs,
)


def _session() -> tuple[object, Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def test_job_summary_counts_queue_and_hides_lease_data() -> None:
    engine, session = _session()
    try:
        session.add_all(
            [
                BackgroundJob(job_type="process_import_batch", resource_id="1"),
                BackgroundJob(
                    job_type="process_import_batch",
                    resource_id="2",
                    status=JobStatus.RUNNING,
                    lease_token="lease-value-not-returned",
                    error_code="temporary_io",
                ),
                BackgroundJob(
                    job_type="mail_sync",
                    resource_id="mail-run",
                    status=JobStatus.FAILED,
                    error_code="mail_sync_failed",
                ),
            ]
        )
        session.commit()

        summary = summarize_jobs(session)

        assert summary["queue"]["pending"] == 1
        assert summary["queue"]["running"] == 1
        assert summary["queue"]["failed"] == 1
        assert summary["queue"]["active"] == 1
        assert summary["queue"]["backlog"] == 1
        assert summary["failed_jobs"][0]["error_code"] == "mail_sync_failed"
        assert "lease_token" not in str(summary)
        assert "lease-value-not-returned" not in str(summary)
    finally:
        session.close()
        engine.dispose()


def test_maintenance_records_failure_without_exception_text_or_secret() -> None:
    engine, session = _session()
    settings = replace(get_settings(), database_url="sqlite+pysqlite:///:memory:")
    try:
        service = MaintenanceService(session, settings)

        def fail_backup(*args: object, **kwargs: object) -> object:
            raise RuntimeError("sensitive-value-not-returned")

        service._run_database_backup = fail_backup  # type: ignore[method-assign]

        result = service.run("database-backup")

        assert isinstance(result, MaintenanceResult)
        assert result.status == "failed"
        assert result.error_code == "maintenance_failed"
        assert "sensitive-value-not-returned" not in str(result.as_dict())
        audit = session.query(AuditLog).one()
        assert audit.result == AuditResult.FAILURE
        # The exception class is recorded for log-grepping; the full
        # traceback stays in the server log so the audit row cannot absorb
        # sensitive str(exc) content.
        assert audit.summary == {
            "command": "database-backup",
            "status": "failed",
            "error_code": "maintenance_failed",
            "error_class": "RuntimeError",
        }
        # Secret from the underlying exception must not leak through audit.
        assert "sensitive-value-not-returned" not in str(audit.summary)
    finally:
        session.close()
        engine.dispose()


def test_schedule_defaults_and_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = ScheduleConfig.from_environment()
    assert defaults.mail_interval_minutes == 5
    assert defaults.backup_interval_hours == 24
    assert defaults.retention_interval_hours == 24
    assert defaults.health_interval_minutes == 1

    monkeypatch.setenv("MAINTENANCE_MAIL_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("MAINTENANCE_BACKUP_INTERVAL_HOURS", "12")
    monkeypatch.setenv("MAINTENANCE_RETENTION_INTERVAL_HOURS", "36")
    monkeypatch.setenv("MAINTENANCE_HEALTH_INTERVAL_MINUTES", "2")
    overridden = ScheduleConfig.from_environment()
    assert overridden.mail_interval_minutes == 10
    assert overridden.backup_interval_hours == 12
    assert overridden.retention_interval_hours == 36
    assert overridden.health_interval_minutes == 2


def test_persisted_mail_interval_overrides_the_five_minute_default() -> None:
    engine, session = _session()
    try:
        session.add(SystemState(id=1, settings={"mail_sync_interval_minutes": 20}))
        session.commit()

        assert ScheduleConfig.from_environment(session).mail_interval_minutes == 20
    finally:
        session.close()
        engine.dispose()


def test_persisted_pause_skips_scheduled_mail_sync() -> None:
    engine, session = _session()
    settings = replace(get_settings(), database_url="sqlite+pysqlite:///:memory:")
    try:
        session.add(SystemState(id=1, settings={"mail_sync_enabled": False}))
        session.commit()

        result = MaintenanceService(session, settings).run("mail-sync")

        assert result.status == "succeeded"
        assert result.summary == {
            "skipped": True,
            "reason": "mail_sync_paused",
        }
    finally:
        session.close()
        engine.dispose()


def test_cli_exposes_one_shot_commands_and_safe_retention_default() -> None:
    parser = build_parser()

    args = parser.parse_args(["source-retention"])

    assert args.command == "source-retention"
    assert args.apply is False
    assert (
        parser.parse_args(
            ["database-backup", "--output-name", "nightly.dump"]
        ).output_name
        == "nightly.dump"
    )


def test_maintenance_success_is_audited_with_public_summary() -> None:
    engine, session = _session()
    settings = replace(get_settings(), database_url="sqlite+pysqlite:///:memory:")
    try:
        service = MaintenanceService(session, settings)
        service._run_job_summary = lambda: {"queue": {"backlog": 0}}  # type: ignore[method-assign]

        result = service.run("job-summary", reason="管理员手工检查")

        assert result.status == "succeeded"
        assert result.summary == {"queue": {"backlog": 0}}
        audit = session.query(AuditLog).one()
        assert audit.action == "system.maintenance"
        assert audit.result == AuditResult.SUCCESS
        assert audit.reason == "管理员手工检查"
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("error_type", [MailConfigurationError, MailConnectionError])
def test_mail_failures_return_controlled_result_and_commit_safe_audit(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    engine, session = _session()
    try:
        service = MaintenanceService(session, get_settings())

        def fail_mail():
            raise error_type("test-secret-must-not-leak")

        monkeypatch.setattr(service, "_run_mail_sync", fail_mail)
        result = service.run("mail-sync")

        assert result.status == "failed"
        assert result.error_code == "mail_not_available"
        with Session(engine) as verification:
            audit = verification.query(AuditLog).one()
            assert audit.result == AuditResult.FAILURE
            assert audit.summary["error_code"] == "mail_not_available"
            assert audit.summary["error_class"] == error_type.__name__
            assert "test-secret-must-not-leak" not in repr(audit.summary)
    finally:
        session.close()
        engine.dispose()
