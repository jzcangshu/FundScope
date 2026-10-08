from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import AuditResult
from app.db.models import AuditLog
from app.system.backup import (
    BackupService,
    BackupStatus,
    DatabaseBackupAdapter,
    UnconfiguredRemoteSourceBackup,
    UnsafeBackupPathError,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


def test_postgresql_command_never_contains_connection_password(
    tmp_path: Path,
) -> None:
    adapter = DatabaseBackupAdapter(
        "postgresql+psycopg://backup_user:super-secret@db.example:5432/funds?sslmode=require",
        tmp_path,
    )

    command = adapter.build_command("nested/database.dump")

    assert command[0] == "pg_dump"
    assert "--no-password" in command
    assert "super-secret" not in " ".join(command)
    assert "backup_user" in command
    assert str((tmp_path / "nested" / "database.dump").resolve()) in command


def test_backup_target_cannot_escape_configured_root(tmp_path: Path) -> None:
    adapter = DatabaseBackupAdapter(
        "postgresql+psycopg://backup_user@db.example/funds",
        tmp_path / "backups",
    )

    with pytest.raises(UnsafeBackupPathError):
        adapter.build_command("../outside.dump")


def test_sqlite_adapter_makes_a_real_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "application.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample (value) VALUES ('kept')")
        connection.commit()

    adapter = DatabaseBackupAdapter(_sqlite_url(database_path), tmp_path / "backups")
    execution = adapter.execute("application-copy.db")

    assert execution.status == BackupStatus.SUCCEEDED
    assert (
        execution.target_path
        == (tmp_path / "backups" / "application-copy.db").resolve()
    )
    assert execution.size_bytes > 0
    with sqlite3.connect(execution.target_path) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == ("kept",)


def test_backup_service_records_failure_and_keeps_password_out_of_audit(
    session: Session, tmp_path: Path
) -> None:
    captured: list[tuple[str, ...]] = []

    def failed_runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="connection failed for super-secret",
        )

    adapter = DatabaseBackupAdapter(
        "postgresql+psycopg://backup_user:super-secret@db.example/funds",
        tmp_path / "backups",
        runner=failed_runner,
    )
    service = BackupService(session, adapter)

    result = service.run(
        output_name="failed.dump",
        now=datetime(2026, 8, 26, tzinfo=UTC),
    )

    assert result.status == BackupStatus.FAILED
    assert result.error_code == "pg_dump_failed"
    assert captured and "super-secret" not in " ".join(captured[0])
    audit = session.get(AuditLog, result.audit_log_id)
    assert audit is not None
    assert audit.result == AuditResult.FAILURE
    assert "super-secret" not in repr(audit.summary)
    assert service.latest_result() == result


def test_backup_service_records_success_and_recent_result(
    session: Session, tmp_path: Path
) -> None:
    database_path = tmp_path / "application.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sample (value) VALUES ('ok')")
        connection.commit()

    service = BackupService(
        session,
        DatabaseBackupAdapter(_sqlite_url(database_path), tmp_path / "backups"),
    )
    result = service.run(output_name="success.db")

    assert result.status == BackupStatus.SUCCEEDED
    assert result.backup_path is not None and result.backup_path.exists()
    assert service.latest_result() == result
    audit = session.scalar(select(AuditLog).where(AuditLog.id == result.audit_log_id))
    assert audit is not None
    assert audit.result == AuditResult.SUCCESS
    assert audit.summary["remote_source_backup"] == "not_configured"


def test_remote_source_backup_is_explicitly_not_configured() -> None:
    result = UnconfiguredRemoteSourceBackup().backup([1, 2])

    assert result.status == "not_configured"
    assert "not configured" in result.message


@pytest.mark.parametrize(
    "failure",
    ["dump_failed", "timeout", "missing", "empty", "corrupt", "verify_timeout"],
)
def test_failed_or_unverified_backup_never_rotates_existing_recovery_points(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    old_backup = tmp_path / "database-old.dump"
    old_backup.write_bytes(b"existing recovery point")
    os.utime(old_backup, (0, 0))
    deleted: list[Path] = []
    monkeypatch.setattr(Path, "unlink", lambda path, **_kw: deleted.append(path))

    def runner(command):
        if command[0] == "pg_dump":
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if failure == "dump_failed":
                return subprocess.CompletedProcess(command, 1)
            if failure != "missing":
                Path(command[command.index("--file") + 1]).write_bytes(
                    b"" if failure == "empty" else b"invalid archive"
                )
            return subprocess.CompletedProcess(command, 0)
        if failure == "verify_timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 1)

    result = BackupService(
        session,
        DatabaseBackupAdapter(
            "postgresql+psycopg://backup@db/funds", tmp_path, runner=runner
        ),
    ).run(output_name="database-new.dump")

    assert result.status == BackupStatus.FAILED
    assert deleted == []
    assert old_backup.exists()
    audit = session.get(AuditLog, result.audit_log_id)
    assert audit.result == AuditResult.FAILURE
    assert audit.summary["cleanup_deleted_count"] == 0
    assert audit.summary["cleanup_skipped_reason"] == "backup_failed"


def test_successful_archive_is_fully_read_before_scoped_rotation(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_backup = tmp_path / "database-old.dump"
    unrelated = tmp_path / "keep.txt"
    for path in (old_backup, unrelated):
        path.write_bytes(b"old")
        os.utime(path, (0, 0))
    commands = []
    deleted: list[Path] = []
    monkeypatch.setattr(Path, "unlink", lambda path, **_kw: deleted.append(path))

    def runner(command):
        commands.append(tuple(command))
        assert deleted == []
        if command[0] == "pg_dump":
            target = Path(command[command.index("--file") + 1])
            target.write_bytes(b"archive verified by the runner")
            # The new recovery point stays protected regardless of its timestamp.
            os.utime(target, (0, 0))
        return subprocess.CompletedProcess(command, 0)

    result = BackupService(
        session,
        DatabaseBackupAdapter(
            "postgresql+psycopg://backup@db/funds", tmp_path, runner=runner
        ),
    ).run(output_name="database-current.dump")

    assert result.status == BackupStatus.SUCCEEDED
    assert commands[1] == ("pg_restore", "--file", os.devnull, str(result.backup_path))
    assert deleted == [old_backup]
    assert unrelated.exists()
    audit = session.get(AuditLog, result.audit_log_id)
    assert audit.summary["archive_verified"] is True
    assert audit.summary["cleanup_deleted_count"] == 1
