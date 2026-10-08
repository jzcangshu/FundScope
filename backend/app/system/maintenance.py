"""One-shot maintenance orchestration over the existing services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import AuditResult, JobStatus
from app.db.models import AuditLog, BackgroundJob, SystemState
from app.mail import (
    MailConfigurationError,
    MailConnectionError,
    MailService,
    MailSettings,
)
from app.system.backup import BackupService
from app.system.health import queue_summary
from app.system.retention import RetentionService
from app.system.settings import effective_mail_username, mail_sync_enabled

MAINTENANCE_COMMANDS = (
    "mail-sync",
    "database-backup",
    "source-retention",
    "job-summary",
)


def _positive_env(name: str, default: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if 0 < value <= maximum else default


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    mail_interval_minutes: int = 5
    backup_interval_hours: int = 24
    retention_interval_hours: int = 24
    health_interval_minutes: int = 1

    @classmethod
    def from_environment(cls, session: Session | None = None) -> ScheduleConfig:
        mail_default = 5
        if session is not None:
            state = session.get(SystemState, 1)
            persisted = state.settings if state is not None else None
            persisted_mail = (
                persisted.get("mail_sync_interval_minutes")
                if isinstance(persisted, dict)
                else None
            )
            if isinstance(persisted_mail, int) and 1 <= persisted_mail <= 1440:
                mail_default = persisted_mail
        return cls(
            mail_interval_minutes=_positive_env(
                "MAINTENANCE_MAIL_INTERVAL_MINUTES", mail_default, 1440
            ),
            backup_interval_hours=_positive_env(
                "MAINTENANCE_BACKUP_INTERVAL_HOURS", 24, 24 * 365
            ),
            retention_interval_hours=_positive_env(
                "MAINTENANCE_RETENTION_INTERVAL_HOURS", 24, 24 * 365
            ),
            health_interval_minutes=_positive_env(
                "MAINTENANCE_HEALTH_INTERVAL_MINUTES", 1, 1440
            ),
        )


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    command: str
    status: str
    summary: dict[str, object]
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "command": self.command,
            "status": self.status,
            "summary": self.summary,
        }
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


class MaintenanceService:
    """Run one bounded maintenance operation and write one safe audit record."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        actor_user_id: int | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.actor_user_id = actor_user_id

    def run(
        self,
        command: str,
        *,
        dry_run: bool = True,
        output_name: str | None = None,
        reason: str | None = None,
    ) -> MaintenanceResult:
        if command not in MAINTENANCE_COMMANDS:
            raise ValueError(f"unsupported maintenance command: {command}")
        error_class = ""
        try:
            status, summary, error_code = self._dispatch(
                command, dry_run=dry_run, output_name=output_name
            )
        except (MailConfigurationError, MailConnectionError) as exc:
            self.session.rollback()
            status, summary, error_code = "failed", {}, "mail_not_available"
            error_class = type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - maintenance failures are audited safely
            self.session.rollback()
            status, summary, error_code = "failed", {}, "maintenance_failed"
            # Exception messages and tracebacks can contain credentials.
            error_class = type(exc).__name__

        result = MaintenanceResult(command, status, summary, error_code)
        audit_summary: dict[str, object] = {"command": command, "status": status}
        if status == "failed":
            audit_summary["error_code"] = error_code or "maintenance_failed"
            if error_class:
                audit_summary["error_class"] = error_class
        else:
            audit_summary.update(summary)
        self.session.add(
            AuditLog(
                actor_user_id=self.actor_user_id,
                action="system.maintenance",
                resource_type="maintenance",
                resource_id=uuid4().hex,
                summary=audit_summary,
                reason=reason,
                result=AuditResult.SUCCESS
                if status == "succeeded"
                else AuditResult.FAILURE,
            )
        )
        self.session.commit()
        return result

    def _dispatch(
        self,
        command: str,
        *,
        dry_run: bool,
        output_name: str | None,
    ) -> tuple[str, dict[str, object], str | None]:
        if command == "mail-sync":
            return self._run_mail_sync()
        if command == "database-backup":
            return self._run_database_backup(output_name)
        if command == "source-retention":
            return self._run_source_retention(dry_run=dry_run)
        return "succeeded", self._run_job_summary(), None

    def _run_mail_sync(self) -> tuple[str, dict[str, object], str | None]:
        if not mail_sync_enabled(self.session):
            return "succeeded", {"skipped": True, "reason": "mail_sync_paused"}, None
        mail_settings = MailSettings.from_environment(
            username_override=effective_mail_username(self.session)
        )
        result = MailService.from_app_settings(
            self.session,
            self.settings,
            mail_settings,
        ).sync(self.actor_user_id)
        summary = {
            key: value
            for key, value in result.as_dict().items()
            if key != "run_id" and key != "errors"
        }
        return (
            result.status,
            summary,
            "mail_sync_failed" if result.status == "failed" else None,
        )

    def _run_database_backup(
        self, output_name: str | None = None
    ) -> tuple[str, dict[str, object], str | None]:
        result = BackupService.from_settings(
            self.session, self.settings, actor_user_id=self.actor_user_id
        ).run(output_name=output_name)
        summary = {
            "backup_name": result.backup_path.name
            if result.backup_path is not None
            else None,
            "size_bytes": result.size_bytes,
        }
        return result.status.value, summary, result.error_code

    def _run_source_retention(
        self, *, dry_run: bool
    ) -> tuple[str, dict[str, object], str | None]:
        result = RetentionService.from_settings(
            self.session, self.settings, actor_user_id=self.actor_user_id
        ).run(dry_run=dry_run)
        summary = {
            "dry_run": result.dry_run,
            "retention_days": result.retention_days,
            "retention_source": result.retention_source,
            "candidate_count": result.candidate_count,
            "deleted_count": result.deleted_count,
            "total_size": result.total_size,
            "deleted_size": result.deleted_size,
            "skipped_reasons": result.skipped_reasons,
            "error_count": len(result.errors),
        }
        status = "failed" if result.errors else "succeeded"
        return status, summary, "source_retention_failed" if result.errors else None

    def _run_job_summary(self) -> dict[str, object]:
        return summarize_jobs(self.session)


def summarize_jobs(session: Session) -> dict[str, object]:
    """Return queue counts and a bounded list of failed jobs without lease tokens."""

    queue = queue_summary(session)
    failed_jobs = list(
        session.scalars(
            select(BackgroundJob)
            .where(BackgroundJob.status == JobStatus.FAILED)
            .order_by(BackgroundJob.finished_at.desc(), BackgroundJob.id.desc())
            .limit(20)
        )
    )
    return {
        "queue": queue,
        "failed_jobs": [
            {
                "id": job.id,
                "type": job.job_type,
                "status": str(getattr(job.status, "value", job.status)),
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "error_code": job.error_code,
                "finished_at": job.finished_at.astimezone(UTC).isoformat()
                if job.finished_at is not None
                else None,
            }
            for job in failed_jobs
        ],
    }


__all__ = [
    "MAINTENANCE_COMMANDS",
    "MaintenanceResult",
    "MaintenanceService",
    "ScheduleConfig",
    "summarize_jobs",
]
