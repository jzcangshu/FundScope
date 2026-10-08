from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import AuditResult, ImportBatchStatus, JobStatus, ValuationStatus
from app.db.models import (
    AuditLog,
    BackgroundJob,
    Fund,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
    SystemState,
    ValuationVersion,
)
from app.system.retention import RetentionService
from app.system.settings import update_settings


def _source(
    session: Session,
    root: Path,
    filename: str,
    *,
    created_at: datetime = datetime(2025, 8, 26, tzinfo=UTC),
    object_name: str | None = None,
) -> SourceFile:
    name = object_name or filename
    if object_name is None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(filename.encode("utf-8"))
    source_file = SourceFile(
        original_filename=filename,
        file_hash=hashlib.sha256(filename.encode("utf-8")).hexdigest(),
        file_size=len(filename.encode("utf-8")),
        file_extension=".xlsx",
        source_type="upload",
        object_name=name,
        created_at=created_at,
    )
    session.add(source_file)
    session.flush()
    return source_file


def _backup_audit(session: Session, source_file: SourceFile) -> None:
    session.add(
        AuditLog(
            action="backup.source_file",
            resource_type="source_file",
            resource_id=str(source_file.id),
            result=AuditResult.SUCCESS,
        )
    )
    session.flush()


def test_default_retention_is_one_year_and_uses_a_rolling_boundary(
    session: Session, tmp_path: Path
) -> None:
    boundary = _source(session, tmp_path, "boundary.xlsx")
    not_due = _source(
        session,
        tmp_path,
        "not-due.xlsx",
        created_at=datetime(2025, 8, 27, tzinfo=UTC),
    )
    service = RetentionService(
        session,
        storage_root=tmp_path,
        backup_checker=lambda source_file: True,
    )

    before_boundary = service.run(as_of=date(2026, 8, 25))
    assert before_boundary.candidate_count == 0
    assert boundary.id is not None

    at_boundary = service.run(as_of=date(2026, 8, 26), dry_run=False)

    assert at_boundary.candidate_count == 1
    assert at_boundary.deleted_count == 1
    assert not (tmp_path / "boundary.xlsx").exists()
    assert (tmp_path / "not-due.xlsx").exists()
    assert session.get(SourceFile, boundary.id) is not None
    assert session.get(SourceFile, not_due.id) is not None


def test_dry_run_keeps_object_and_standardized_rows_and_writes_log(
    session: Session, tmp_path: Path
) -> None:
    source_file = _source(session, tmp_path, "standardized.xlsx")
    fund = Fund(standard_name="测试产品", status="active")
    session.add(fund)
    session.flush()
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=date(2025, 8, 26),
        version_no=1,
        source_file_id=source_file.id,
        status=ValuationStatus.PUBLISHED,
    )
    session.add(version)
    session.flush()

    result = RetentionService(
        session,
        storage_root=tmp_path,
        backup_checker=lambda source_file: True,
    ).run(as_of=date(2026, 8, 26))

    assert result.dry_run is True
    assert result.candidate_count == 1
    assert result.deleted_count == 0
    assert result.total_size == source_file.file_size
    assert (tmp_path / "standardized.xlsx").exists()
    assert session.get(SourceFile, source_file.id) is not None
    assert session.get(ValuationVersion, version.id) is not None

    cleanup_log = session.get(AuditLog, result.audit_log_id)
    assert cleanup_log is not None
    assert cleanup_log.action == "system.source_cleanup"
    assert cleanup_log.summary == {
        "as_of": "2026-08-26",
        "dry_run": True,
        "candidate_count": 1,
        "total_size": source_file.file_size,
        "planned_delete_count": 1,
        "deleted_count": 0,
        "deleted_size": 0,
        "skipped_reasons": {},
        "errors": [],
        "retention_days": 365,
        "retention_source": "explicit",
    }


@pytest.mark.parametrize("dry_run", [True, False])
def test_persisted_retention_applies_to_new_sessions_and_cleanup_modes(
    session: Session, tmp_path: Path, dry_run: bool
) -> None:
    settings = replace(
        get_settings(), source_storage_dir=str(tmp_path), source_retention_days=365
    )
    _source(session, tmp_path, "must-retain.xlsx")
    update_settings(session, settings, {"source_retention_days": 730})
    session.commit()

    with Session(session.get_bind()) as fresh_session:
        service = RetentionService.from_settings(fresh_session, settings)
        result = service.run(as_of=date(2026, 8, 26), dry_run=dry_run)

        assert service.retention_days == 730
        assert result.retention_days == 730
        assert result.retention_source == "database"
        assert result.candidate_count == 0
        audit = fresh_session.get(AuditLog, result.audit_log_id)
        assert audit.summary["retention_days"] == 730
        assert audit.summary["retention_source"] == "database"
    assert (tmp_path / "must-retain.xlsx").exists()


@pytest.mark.parametrize("persisted", [None, -1, True, "730"])
def test_retention_falls_back_to_runtime_for_absent_or_invalid_override(
    session: Session, tmp_path: Path, persisted: object
) -> None:
    settings = replace(
        get_settings(), source_storage_dir=str(tmp_path), source_retention_days=500
    )
    if persisted is not None:
        session.add(SystemState(id=1, settings={"source_retention_days": persisted}))
        session.flush()

    assert RetentionService.from_settings(session, settings).retention_days == 500


def test_cleanup_skips_review_failed_locked_and_unbacked_files(
    session: Session, tmp_path: Path
) -> None:
    review = _source(session, tmp_path, "review.xlsx")
    failed = _source(session, tmp_path, "failed.xlsx")
    locked = _source(session, tmp_path, "locked.xlsx")
    unbacked = _source(session, tmp_path, "unbacked.xlsx")

    fund = Fund(standard_name="待复核产品", status="active")
    session.add(fund)
    session.flush()
    session.add(
        ValuationVersion(
            fund_id=fund.id,
            valuation_date=date(2025, 8, 26),
            version_no=1,
            source_file_id=review.id,
            status=ValuationStatus.PENDING_REVIEW,
        )
    )
    batch = ImportBatch(
        source_type="upload",
        status=ImportBatchStatus.FAILED,
        file_count=1,
    )
    session.add(batch)
    session.flush()
    session.add(
        ImportBatchFile(batch_id=batch.id, source_file_id=failed.id, duplicate=False)
    )
    session.add(
        BackgroundJob(
            job_type="process_import_batch",
            resource_id=str(batch.id),
            status=JobStatus.FAILED,
        )
    )
    session.add(
        AuditLog(
            action="source_file.audit_lock",
            resource_type="source_file",
            resource_id=str(locked.id),
            summary={"audit_locked": True},
            result=AuditResult.SUCCESS,
        )
    )
    session.flush()

    result = RetentionService(
        session,
        storage_root=tmp_path,
        backup_checker=lambda source_file: source_file.id != unbacked.id,
    ).run(as_of=date(2026, 8, 26), dry_run=False)

    assert result.candidate_count == 4
    assert result.deleted_count == 0
    assert result.skipped_reasons == {
        "pending_review_reference": 1,
        "failed_task_reference": 1,
        "audit_locked": 1,
        "backup_incomplete": 1,
    }
    assert all(
        (tmp_path / name).exists()
        for name in [
            "review.xlsx",
            "failed.xlsx",
            "locked.xlsx",
            "unbacked.xlsx",
        ]
    )


def test_cleanup_batches_safety_data_for_multiple_candidates(
    session: Session, tmp_path: Path
) -> None:
    deletable = _source(session, tmp_path, "deletable.xlsx")
    review = _source(session, tmp_path, "review-batched.xlsx")
    active_task = _source(session, tmp_path, "active-task.xlsx")
    _backup_audit(session, deletable)

    fund = Fund(standard_name="批量复核产品", status="active")
    session.add(fund)
    session.flush()
    session.add(
        ValuationVersion(
            fund_id=fund.id,
            valuation_date=date(2025, 8, 26),
            version_no=1,
            source_file_id=review.id,
            status=ValuationStatus.PENDING_REVIEW,
        )
    )
    batch = ImportBatch(
        source_type="upload",
        status=ImportBatchStatus.PROCESSING,
        file_count=1,
    )
    session.add(batch)
    session.flush()
    session.add(
        ImportBatchFile(
            batch_id=batch.id,
            source_file_id=active_task.id,
            duplicate=False,
        )
    )
    session.add(
        BackgroundJob(
            job_type="process_import_batch",
            resource_id=str(batch.id),
            status=JobStatus.RUNNING,
        )
    )
    session.flush()

    select_statements: list[str] = []

    def count_selects(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        result = RetentionService(session, storage_root=tmp_path).run(
            as_of=date(2026, 8, 26), dry_run=False
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert len(select_statements) == 5
    assert result.candidate_count == 3
    assert result.deleted_count == 1
    assert result.skipped_reasons == {
        "pending_review_reference": 1,
        "active_task_reference": 1,
    }
    assert not (tmp_path / "deletable.xlsx").exists()
    assert (tmp_path / "review-batched.xlsx").exists()
    assert (tmp_path / "active-task.xlsx").exists()


def test_completed_source_backup_audit_allows_delete_but_missing_backup_does_not(
    session: Session, tmp_path: Path
) -> None:
    backed_up = _source(session, tmp_path, "backed-up.xlsx")
    not_backed_up = _source(session, tmp_path, "not-backed-up.xlsx")
    _backup_audit(session, backed_up)

    result = RetentionService(session, storage_root=tmp_path).run(
        as_of=date(2026, 8, 26), dry_run=False
    )

    assert result.deleted_count == 1
    assert not (tmp_path / "backed-up.xlsx").exists()
    assert (tmp_path / "not-backed-up.xlsx").exists()
    assert result.skipped_reasons == {"backup_incomplete": 1}
    assert session.get(SourceFile, backed_up.id) is not None
    assert session.get(SourceFile, not_backed_up.id) is not None


def test_cleanup_rejects_object_path_outside_storage_root(
    session: Session, tmp_path: Path
) -> None:
    storage_root = tmp_path / "source"
    storage_root.mkdir()
    outside = tmp_path / "outside.xlsx"
    outside.write_bytes(b"must remain")
    source_file = _source(
        session,
        storage_root,
        "outside.xlsx",
        object_name="../outside.xlsx",
    )

    result = RetentionService(
        session,
        storage_root=storage_root,
        backup_checker=lambda source_file: True,
    ).run(as_of=date(2026, 8, 26), dry_run=False)

    assert result.deleted_count == 0
    assert result.skipped_reasons == {"unsafe_path": 1}
    assert "unsafe_path" in result.errors[0]
    assert outside.read_bytes() == b"must remain"
    assert session.get(SourceFile, source_file.id) is not None


def test_retention_and_backup_directories_are_configurable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SOURCE_RETENTION_DAYS", "30")
    monkeypatch.setenv("DATABASE_BACKUP_DIR", str(tmp_path / "backups"))

    settings = get_settings()

    assert settings.source_retention_days == 30
    assert settings.database_backup_dir == str(tmp_path / "backups")
