"""Safe, database-aware cleanup of immutable source-file objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DEFAULT_SOURCE_RETENTION_DAYS, Settings
from app.db.base import AuditResult, JobStatus, ValuationStatus
from app.db.models import (
    AuditLog,
    BackgroundJob,
    ImportBatchFile,
    SourceFile,
    ValuationVersion,
)
from app.imports.storage import UnsafeStoragePathError, resolve_in_root
from app.system.settings import effective_settings

ACTIVE_TASK_STATUSES = frozenset(
    {
        JobStatus.PENDING.value,
        JobStatus.RUNNING.value,
        JobStatus.RETRY_DUE.value,
    }
)
REVIEW_AUDIT_ACTIONS = frozenset(
    {"import.review_required", "source_file.review_required", "review.required"}
)
FAILED_AUDIT_ACTIONS = frozenset(
    {"import.file_failed", "source_file.task_failed", "task.failed"}
)
SOURCE_BACKUP_AUDIT_ACTIONS = frozenset(
    {
        "backup.source_file",
        "source_file.backup_completed",
        "system.source_backup",
    }
)
AUDIT_LOCK_ACTIONS = frozenset(
    {
        "audit.lock",
        "audit.locked",
        "audit.dispute",
        "source_file.audit_lock",
        "source_file.audit_locked",
        "source_file.audit_dispute",
        "source_file.lock",
        "source_file.locked",
    }
)
AUDIT_UNLOCK_ACTIONS = frozenset(
    {
        "audit.unlock",
        "audit.unlocked",
        "source_file.audit_unlock",
        "source_file.audit_unlocked",
        "source_file.unlock",
        "source_file.unlocked",
    }
)


class SourceBackupChecker(Protocol):
    """Return whether the raw object has a completed source-level backup."""

    def __call__(self, source_file: SourceFile, /) -> bool: ...


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Summary of one retention pass, including dry-run results."""

    as_of: date
    retention_days: int
    dry_run: bool
    candidate_count: int
    deleted_count: int
    total_size: int
    deleted_size: int
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    audit_log_id: int | None = None
    retention_source: str = "explicit"

    @property
    def skipped(self) -> dict[str, int]:
        """Compatibility alias for callers that use the shorter field name."""

        return self.skipped_reasons


@dataclass(frozen=True, slots=True)
class _SafetyDecision:
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _SafetyContext:
    """Batch-loaded safety data for one retention run."""

    audits_by_source_file_id: dict[int, tuple[AuditLog, ...]]
    valuation_statuses_by_source_file_id: dict[int, tuple[str, ...]]
    batch_ids_by_source_file_id: dict[int, tuple[int, ...]]
    task_statuses_by_batch_id: dict[str, tuple[str, ...]]
    failed_audit_source_file_ids: frozenset[int]
    latest_backup_audits_by_source_file_id: dict[int, AuditLog]

    @classmethod
    def load(cls, session: Session, source_file_ids: tuple[int, ...]) -> _SafetyContext:
        """Load every database fact used by safety decisions in batches."""

        if not source_file_ids:
            return cls({}, {}, {}, {}, frozenset(), {})

        resource_ids = tuple(str(source_file_id) for source_file_id in source_file_ids)
        source_id_by_resource_id = dict(zip(resource_ids, source_file_ids))
        source_file_audits = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.resource_type == "source_file",
                AuditLog.resource_id.in_(resource_ids),
            )
            .order_by(AuditLog.id)
        ).all()
        audits_by_source_file_id: dict[int, list[AuditLog]] = {}
        failed_audit_source_file_ids: set[int] = set()
        latest_backup_audits_by_source_file_id: dict[int, AuditLog] = {}
        for audit in source_file_audits:
            if audit.resource_id is None:
                continue
            source_file_id = source_id_by_resource_id[audit.resource_id]
            audits_by_source_file_id.setdefault(source_file_id, []).append(audit)
            if audit.action in FAILED_AUDIT_ACTIONS:
                failed_audit_source_file_ids.add(source_file_id)
            if audit.action in SOURCE_BACKUP_AUDIT_ACTIONS:
                latest_backup_audits_by_source_file_id[source_file_id] = audit

        valuation_rows = session.execute(
            select(ValuationVersion.source_file_id, ValuationVersion.status).where(
                ValuationVersion.source_file_id.in_(source_file_ids)
            )
        ).all()
        valuation_statuses_by_source_file_id: dict[int, list[str]] = {}
        for source_file_id, status in valuation_rows:
            if source_file_id is not None:
                value = (
                    status.value if isinstance(status, ValuationStatus) else str(status)
                )
                valuation_statuses_by_source_file_id.setdefault(
                    source_file_id, []
                ).append(value)

        batch_rows = session.execute(
            select(ImportBatchFile.source_file_id, ImportBatchFile.batch_id)
            .where(ImportBatchFile.source_file_id.in_(source_file_ids))
            .order_by(ImportBatchFile.id)
        ).all()
        batch_ids_by_source_file_id: dict[int, list[int]] = {}
        batch_ids: set[int] = set()
        for source_file_id, batch_id in batch_rows:
            batch_ids_by_source_file_id.setdefault(source_file_id, []).append(batch_id)
            batch_ids.add(batch_id)

        task_statuses_by_batch_id: dict[str, list[str]] = {}
        if batch_ids:
            task_rows = session.execute(
                select(BackgroundJob.resource_id, BackgroundJob.status)
                .where(
                    BackgroundJob.job_type == "process_import_batch",
                    BackgroundJob.resource_id.in_(
                        str(batch_id) for batch_id in batch_ids
                    ),
                )
                .order_by(BackgroundJob.id)
            ).all()
            for resource_id, status in task_rows:
                value = status.value if isinstance(status, JobStatus) else str(status)
                task_statuses_by_batch_id.setdefault(resource_id, []).append(value)

        return cls(
            audits_by_source_file_id={
                source_file_id: tuple(audits)
                for source_file_id, audits in audits_by_source_file_id.items()
            },
            valuation_statuses_by_source_file_id={
                source_file_id: tuple(statuses)
                for source_file_id, statuses in valuation_statuses_by_source_file_id.items()
            },
            batch_ids_by_source_file_id={
                source_file_id: tuple(batch_ids)
                for source_file_id, batch_ids in batch_ids_by_source_file_id.items()
            },
            task_statuses_by_batch_id={
                resource_id: tuple(statuses)
                for resource_id, statuses in task_statuses_by_batch_id.items()
            },
            failed_audit_source_file_ids=frozenset(failed_audit_source_file_ids),
            latest_backup_audits_by_source_file_id=latest_backup_audits_by_source_file_id,
        )


class RetentionService:
    """Plan and optionally remove expired raw objects without touching rows."""

    def __init__(
        self,
        session: Session,
        *,
        storage_root: Path,
        retention_days: int = DEFAULT_SOURCE_RETENTION_DAYS,
        retention_source: str = "explicit",
        backup_checker: SourceBackupChecker | None = None,
        actor_user_id: int | None = None,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        self.session = session
        self.storage_root = Path(storage_root).resolve()
        self.retention_days = retention_days
        self.retention_source = retention_source
        self.backup_checker = backup_checker
        self.actor_user_id = actor_user_id

    @classmethod
    def from_settings(
        cls,
        session: Session,
        settings: Settings,
        *,
        backup_checker: SourceBackupChecker | None = None,
        actor_user_id: int | None = None,
    ) -> RetentionService:
        retention = effective_settings(session, settings)["source_retention_days"]
        return cls(
            session,
            storage_root=Path(settings.source_storage_dir),
            retention_days=cast(int, retention["value"]),
            retention_source=str(retention["source"]),
            backup_checker=backup_checker,
            actor_user_id=actor_user_id,
        )

    def run(
        self,
        *,
        as_of: date | None = None,
        dry_run: bool = True,
    ) -> CleanupResult:
        """Create a cleanup audit record and delete only approved objects."""

        current_date = as_of or datetime.now(UTC).date()
        candidates = [
            source_file
            for source_file in self.session.scalars(
                select(SourceFile).order_by(SourceFile.id)
            ).all()
            if self._retention_expired(source_file, current_date)
        ]
        safety_context = _SafetyContext.load(
            self.session, tuple(source_file.id for source_file in candidates)
        )
        skipped_reasons: dict[str, int] = {}
        errors: list[str] = []
        planned: list[tuple[SourceFile, Path]] = []
        total_size = 0

        for source_file in candidates:
            total_size += max(source_file.file_size, 0)
            decision = self._safety_decision(source_file, safety_context)
            if decision.reason is not None:
                skipped_reasons[decision.reason] = (
                    skipped_reasons.get(decision.reason, 0) + 1
                )
                if decision.error is not None:
                    errors.append(decision.error)
                continue

            try:
                object_path = self._safe_object_path(source_file)
            except UnsafeStoragePathError:
                skipped_reasons["unsafe_path"] = (
                    skipped_reasons.get("unsafe_path", 0) + 1
                )
                errors.append(f"source_file:{source_file.id}:unsafe_path")
                continue

            object_link = self.storage_root / Path(source_file.object_name)
            if object_link.is_symlink():
                skipped_reasons["unsafe_path"] = (
                    skipped_reasons.get("unsafe_path", 0) + 1
                )
                errors.append(f"source_file:{source_file.id}:symlink_object")
                continue
            if not object_path.exists():
                errors.append(f"source_file:{source_file.id}:object_not_found")
                continue
            if not object_path.is_file():
                errors.append(f"source_file:{source_file.id}:object_not_regular_file")
                continue
            planned.append((source_file, object_path))

        deleted_count = 0
        deleted_size = 0
        summary = self._summary(
            as_of=current_date,
            dry_run=dry_run,
            candidate_count=len(candidates),
            total_size=total_size,
            planned_count=len(planned),
            deleted_count=deleted_count,
            deleted_size=deleted_size,
            skipped_reasons=skipped_reasons,
            errors=errors,
        )
        cleanup_log = AuditLog(
            actor_user_id=self.actor_user_id,
            action="system.source_cleanup",
            resource_type="source_storage",
            summary=summary,
            result=AuditResult.FAILURE if errors else AuditResult.SUCCESS,
        )
        self.session.add(cleanup_log)
        self.session.flush()

        if not dry_run:
            for source_file, object_path in planned:
                try:
                    object_path.unlink()
                except OSError:
                    errors.append(f"source_file:{source_file.id}:delete_failed")
                    continue
                deleted_count += 1
                deleted_size += max(source_file.file_size, 0)

            summary = self._summary(
                as_of=current_date,
                dry_run=dry_run,
                candidate_count=len(candidates),
                total_size=total_size,
                planned_count=len(planned),
                deleted_count=deleted_count,
                deleted_size=deleted_size,
                skipped_reasons=skipped_reasons,
                errors=errors,
            )
            cleanup_log.summary = summary
            cleanup_log.result = AuditResult.FAILURE if errors else AuditResult.SUCCESS
            self.session.flush()

        return CleanupResult(
            as_of=current_date,
            retention_days=self.retention_days,
            retention_source=self.retention_source,
            dry_run=dry_run,
            candidate_count=len(candidates),
            deleted_count=deleted_count,
            total_size=total_size,
            deleted_size=deleted_size,
            skipped_reasons=dict(skipped_reasons),
            errors=tuple(errors),
            audit_log_id=cleanup_log.id,
        )

    def cleanup(
        self,
        *,
        as_of: date | None = None,
        dry_run: bool = True,
    ) -> CleanupResult:
        """Alias for ``run`` used by scheduled service callers."""

        return self.run(as_of=as_of, dry_run=dry_run)

    def _retention_expired(self, source_file: SourceFile, as_of: date) -> bool:
        if source_file.retention_expires_on is not None:
            return source_file.retention_expires_on <= as_of
        created_at = source_file.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        created_on = created_at.astimezone(UTC).date()
        return created_on + timedelta(days=self.retention_days) <= as_of

    def _safety_decision(
        self, source_file: SourceFile, safety_context: _SafetyContext
    ) -> _SafetyDecision:
        audits = safety_context.audits_by_source_file_id.get(source_file.id, ())
        if self._has_pending_review(source_file, audits, safety_context):
            return _SafetyDecision("pending_review_reference")
        task_reason = self._task_reference_reason(source_file, safety_context)
        if task_reason is not None:
            return _SafetyDecision(task_reason)
        if self._is_audit_locked(audits):
            return _SafetyDecision("audit_locked")
        try:
            backed_up = (
                self.backup_checker(source_file)
                if self.backup_checker is not None
                else self._has_source_backup_audit(source_file.id, safety_context)
            )
        except Exception:  # noqa: BLE001 - a failed checker must fail closed
            return _SafetyDecision(
                "backup_check_failed",
                f"source_file:{source_file.id}:backup_check_failed",
            )
        if not backed_up:
            return _SafetyDecision("backup_incomplete")
        return _SafetyDecision()

    def _has_pending_review(
        self,
        source_file: SourceFile,
        audits: tuple[AuditLog, ...],
        safety_context: _SafetyContext,
    ) -> bool:
        if (
            ValuationStatus.PENDING_REVIEW.value
            in safety_context.valuation_statuses_by_source_file_id.get(
                source_file.id, ()
            )
        ):
            return True
        return any(
            self._normalized_action(audit.action) in REVIEW_AUDIT_ACTIONS
            for audit in audits
        )

    def _task_reference_reason(
        self, source_file: SourceFile, safety_context: _SafetyContext
    ) -> str | None:
        batch_ids = safety_context.batch_ids_by_source_file_id.get(source_file.id, ())
        if not batch_ids:
            return self._failed_audit_reason(source_file.id, safety_context)
        statuses = (
            status
            for batch_id in batch_ids
            for status in safety_context.task_statuses_by_batch_id.get(
                str(batch_id), ()
            )
        )
        for status in statuses:
            if status == JobStatus.FAILED.value:
                return "failed_task_reference"
            if status in ACTIVE_TASK_STATUSES:
                return "active_task_reference"
        return self._failed_audit_reason(source_file.id, safety_context)

    def _failed_audit_reason(
        self, source_file_id: int, safety_context: _SafetyContext
    ) -> str | None:
        return (
            "failed_task_reference"
            if source_file_id in safety_context.failed_audit_source_file_ids
            else None
        )

    def _is_audit_locked(self, audits: tuple[AuditLog, ...]) -> bool:
        locked = False
        for audit in audits:
            action = self._normalized_action(audit.action)
            if action in AUDIT_UNLOCK_ACTIONS or self._action_has_unlock_marker(action):
                locked = False
            elif action in AUDIT_LOCK_ACTIONS or self._action_has_lock_marker(action):
                locked = True
            else:
                flag = self._audit_lock_flag(audit.summary)
                if flag is not None:
                    locked = flag
        return locked

    def _has_source_backup_audit(
        self, source_file_id: int, safety_context: _SafetyContext
    ) -> bool:
        latest = safety_context.latest_backup_audits_by_source_file_id.get(
            source_file_id
        )
        return latest is not None and latest.result == AuditResult.SUCCESS

    def _safe_object_path(self, source_file: SourceFile) -> Path:
        object_path = resolve_in_root(self.storage_root, source_file.object_name)
        if object_path == self.storage_root:
            raise UnsafeStoragePathError(source_file.object_name)
        return object_path

    @staticmethod
    def _normalized_action(action: str) -> str:
        return action.strip().casefold()

    @staticmethod
    def _action_has_lock_marker(action: str) -> bool:
        return (
            any(
                marker in action
                for marker in (
                    "audit.lock",
                    "audit_lock",
                    "audit.locked",
                    "audit_locked",
                )
            )
            or "audit.dispute" in action
            or "audit_dispute" in action
        )

    @staticmethod
    def _action_has_unlock_marker(action: str) -> bool:
        return any(
            marker in action
            for marker in (
                "audit.unlock",
                "audit_unlock",
                "audit.unlocked",
                "audit_unlocked",
            )
        )

    @staticmethod
    def _audit_lock_flag(summary: dict[str, Any] | None) -> bool | None:
        if not isinstance(summary, dict):
            return None
        for key in ("audit_locked", "locked", "disputed"):
            value = summary.get(key)
            if isinstance(value, bool):
                return value
        return None

    def _summary(
        self,
        *,
        as_of: date,
        dry_run: bool,
        candidate_count: int,
        total_size: int,
        planned_count: int,
        deleted_count: int,
        deleted_size: int,
        skipped_reasons: dict[str, int],
        errors: list[str],
    ) -> dict[str, object]:
        return {
            "as_of": as_of.isoformat(),
            "retention_days": self.retention_days,
            "retention_source": self.retention_source,
            "dry_run": dry_run,
            "candidate_count": candidate_count,
            "total_size": total_size,
            "planned_delete_count": planned_count,
            "deleted_count": deleted_count,
            "deleted_size": deleted_size,
            "skipped_reasons": dict(skipped_reasons),
            "errors": list(errors),
        }


__all__ = [
    "CleanupResult",
    "RetentionService",
    "SourceBackupChecker",
]
