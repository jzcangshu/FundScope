"""Operational health snapshots backed by existing database state and audits."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import JobStatus
from app.db.models import AuditLog, BackgroundJob, SystemState

MAINTENANCE_STATE_KEY = "_maintenance"
DEFAULT_WORKER_STALE_SECONDS = 180
DEFAULT_DISK_WARNING_PERCENT = 70
DEFAULT_DISK_CRITICAL_PERCENT = 90


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _positive_env(name: str, default: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if 0 < value <= maximum else default


def _percentage_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if 0 <= value <= 100 else default


def _maintenance_state(session: Session) -> tuple[SystemState, dict[str, Any]]:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    settings = state.settings if isinstance(state.settings, dict) else {}
    raw_maintenance = settings.get(MAINTENANCE_STATE_KEY)
    maintenance = raw_maintenance if isinstance(raw_maintenance, dict) else {}
    return state, dict(maintenance)


def record_worker_heartbeat(
    session: Session,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> None:
    """Persist a non-sensitive timestamp for one worker instance."""

    current_time = now or datetime.now(UTC)
    state, maintenance = _maintenance_state(session)
    raw_workers = maintenance.get("workers")
    workers = dict(raw_workers) if isinstance(raw_workers, dict) else {}
    workers[worker_id] = {"last_seen_at": _as_utc(current_time).isoformat()}
    maintenance["workers"] = workers
    current_settings = state.settings if isinstance(state.settings, dict) else {}
    state.settings = {**current_settings, MAINTENANCE_STATE_KEY: maintenance}
    session.flush()


def _worker_summary(session: Session, now: datetime) -> dict[str, object]:
    state = session.get(SystemState, 1)
    settings = (
        state.settings if state is not None and isinstance(state.settings, dict) else {}
    )
    maintenance = settings.get(MAINTENANCE_STATE_KEY)
    raw_workers = maintenance.get("workers") if isinstance(maintenance, dict) else None
    workers = raw_workers if isinstance(raw_workers, dict) else {}
    latest: tuple[str, datetime] | None = None
    for raw_worker_id, raw_heartbeat in workers.items():
        if not isinstance(raw_worker_id, str) or not isinstance(raw_heartbeat, dict):
            continue
        raw_timestamp = raw_heartbeat.get("last_seen_at")
        if not isinstance(raw_timestamp, str):
            continue
        try:
            timestamp = _as_utc(datetime.fromisoformat(raw_timestamp))
        except ValueError:
            continue
        if latest is None or timestamp > latest[1]:
            latest = (raw_worker_id, timestamp)

    if latest is None:
        return {
            "status": "unknown",
            "worker_id": None,
            "last_seen_at": None,
            "age_seconds": None,
        }
    age_seconds = max(0, int((_as_utc(now) - latest[1]).total_seconds()))
    stale_after = _positive_env(
        "WORKER_HEARTBEAT_STALE_SECONDS", DEFAULT_WORKER_STALE_SECONDS, 86_400
    )
    return {
        "status": "healthy" if age_seconds <= stale_after else "stale",
        "worker_id": latest[0],
        "last_seen_at": latest[1].isoformat(),
        "age_seconds": age_seconds,
    }


def queue_summary(session: Session) -> dict[str, object]:
    """Return bounded queue counts without exposing leases or resource data."""

    rows = session.execute(
        select(BackgroundJob.status, func.count(BackgroundJob.id)).group_by(
            BackgroundJob.status
        )
    ).all()
    counts = {status.value: 0 for status in JobStatus}
    for status, count in rows:
        counts[_enum_value(status)] = int(count)
    backlog = counts[JobStatus.PENDING.value] + counts[JobStatus.RETRY_DUE.value]
    return {**counts, "active": counts[JobStatus.RUNNING.value], "backlog": backlog}


def _maintenance_runs(session: Session) -> dict[str, object | None]:
    audits = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.action == "system.maintenance",
                AuditLog.resource_type == "maintenance",
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(20)
        )
    )
    successful = next(
        (audit for audit in audits if _enum_value(audit.result) == "success"), None
    )
    failed = next(
        (audit for audit in audits if _enum_value(audit.result) == "failure"), None
    )

    def public_run(audit: AuditLog | None) -> dict[str, object] | None:
        if audit is None or not isinstance(audit.summary, dict):
            return None
        return {
            "command": audit.summary.get("command"),
            "status": audit.summary.get("status"),
            "error_code": audit.summary.get("error_code"),
            "completed_at": _iso(audit.created_at),
        }

    return {"last_success": public_run(successful), "last_failure": public_run(failed)}


def _backup_summary(session: Session) -> dict[str, object | None]:
    audit = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "system.database_backup",
            AuditLog.resource_type == "database",
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
    )
    if audit is None or not isinstance(audit.summary, dict):
        return {
            "status": "unknown",
            "backup_name": None,
            "size_bytes": None,
            "completed_at": None,
        }
    summary = audit.summary
    raw_name = summary.get("backup_name")
    backup_name = (
        Path(raw_name).name if isinstance(raw_name, str) and raw_name else None
    )
    raw_size = summary.get("size_bytes")
    return {
        "status": str(summary.get("status", "failed")),
        "backup_name": backup_name,
        "size_bytes": raw_size if isinstance(raw_size, int) else 0,
        "error_code": summary.get("error_code")
        if isinstance(summary.get("error_code"), str)
        else None,
        "completed_at": _iso(audit.created_at),
    }


def _disk_entry(path: str, *, warning: int, critical: int) -> dict[str, object]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"status": "unavailable", "error_code": "disk_usage_unavailable"}
    used_percent = round((usage.used / usage.total) * 100, 2) if usage.total else 100.0
    status = "ok"
    if used_percent >= critical:
        status = "critical"
    elif used_percent >= warning:
        status = "warning"
    return {
        "status": status,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": used_percent,
    }


def _disk_summary(settings: Settings) -> dict[str, dict[str, object]]:
    warning = _percentage_env(
        "MAINTENANCE_DISK_WARNING_PERCENT", DEFAULT_DISK_WARNING_PERCENT
    )
    critical = _percentage_env(
        "MAINTENANCE_DISK_CRITICAL_PERCENT", DEFAULT_DISK_CRITICAL_PERCENT
    )
    critical = max(critical, warning)
    return {
        "source_storage": _disk_entry(
            settings.source_storage_dir, warning=warning, critical=critical
        ),
        "database_backups": _disk_entry(
            settings.database_backup_dir, warning=warning, critical=critical
        ),
        "upload_temp": _disk_entry(
            settings.upload_temp_dir, warning=warning, critical=critical
        ),
    }


def operational_summary(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return an authenticated operational snapshot without secrets or raw paths."""

    current_time = now or datetime.now(UTC)
    try:
        session.execute(select(1))
        database = {"status": "ok"}
    except SQLAlchemyError:
        return {
            "status": "critical",
            "database": {"status": "unavailable"},
            "worker": {
                "status": "unknown",
                "worker_id": None,
                "last_seen_at": None,
                "age_seconds": None,
            },
            "queue": {
                "pending": None,
                "running": None,
                "retry_due": None,
                "failed": None,
                "succeeded": None,
                "backlog": None,
            },
            "maintenance": {"last_success": None, "last_failure": None},
            "backup": {
                "status": "unknown",
                "backup_name": None,
                "size_bytes": None,
                "completed_at": None,
            },
            "disk": _disk_summary(settings),
        }

    worker = _worker_summary(session, current_time)
    queue = queue_summary(session)
    backup = _backup_summary(session)
    disk = _disk_summary(settings)
    maintenance = _maintenance_runs(session)
    statuses = [
        worker["status"],
        backup["status"],
        *[entry["status"] for entry in disk.values()],
    ]
    status = "ok"
    if database["status"] != "ok" or "critical" in statuses:
        status = "critical"
    elif (
        "stale" in statuses
        or "unknown" in statuses
        or "failed" in statuses
        or "warning" in statuses
    ):
        status = "degraded"
    return {
        "status": status,
        "database": database,
        "worker": worker,
        "queue": queue,
        "maintenance": maintenance,
        "backup": backup,
        "disk": disk,
    }


__all__ = ["operational_summary", "queue_summary", "record_worker_heartbeat"]
