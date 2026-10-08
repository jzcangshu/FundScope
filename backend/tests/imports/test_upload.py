import errno
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.config import get_settings
from app.db.base import SourceType
from app.db.models import AuditLog, SourceFile
from app.imports.service import ImportService
from app.imports.storage import (
    InvalidFileError,
    UnsafeStoragePathError,
    resolve_in_root,
    stage_upload,
    store_staged_upload,
)

from .conftest import make_xlsx_bytes


def test_upload_uses_random_object_name_and_hash(
    app_and_engine: tuple[object, object], tmp_path: Path
) -> None:
    app, engine = app_and_engine
    content = make_xlsx_bytes()
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)

        result = service.receive_upload(
            batch.id, "估值表.xlsx", BytesIO(content), actor.id
        )
        session.commit()

        stored_path = (
            Path(app.state.settings.source_storage_dir) / result.source_file.object_name
        )
        assert result.duplicate is False
        assert result.source_file.file_hash == sha256(content).hexdigest()
        assert result.source_file.object_name != "估值表.xlsx"
        assert result.source_file.object_name.endswith(".xlsx")
        assert stored_path.read_bytes() == content


def test_duplicate_hash_is_idempotent_and_temp_file_is_cleaned(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    content = make_xlsx_bytes()
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        first_batch = service.create_batch(SourceType.UPLOAD, actor.id)
        second_batch = service.create_batch(SourceType.UPLOAD, actor.id)

        first = service.receive_upload(
            first_batch.id, "first.xlsx", BytesIO(content), actor.id
        )
        second = service.receive_upload(
            second_batch.id, "second.xlsx", BytesIO(content), actor.id
        )
        session.commit()

        assert second.duplicate is True
        assert second.source_file.id == first.source_file.id
        assert session.scalar(select(func.count(SourceFile.id))) == 1
        assert len(list(Path(app.state.settings.source_storage_dir).iterdir())) == 1
        assert list(Path(app.state.settings.upload_temp_dir).iterdir()) == []
        actions = set(session.scalars(select(AuditLog.action)).all())
        assert {"import.upload", "import.duplicate_file"}.issubset(actions)


def test_rolled_back_upload_removes_formal_storage_object(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    content = make_xlsx_bytes()
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        result = service.receive_upload(
            batch.id, "rolled-back.xlsx", BytesIO(content), actor.id
        )
        stored_path = (
            Path(app.state.settings.source_storage_dir) / result.source_file.object_name
        )
        assert stored_path.exists()
        session.rollback()
        assert not stored_path.exists()
        assert list(Path(app.state.settings.source_storage_dir).glob("*")) == []


def test_valid_xls_ole_header_is_accepted(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    content = bytes.fromhex("D0CF11E0A1B11AE1") + b"legacy workbook"
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)

        result = service.receive_upload(
            batch.id, "legacy.xls", BytesIO(content), actor.id
        )

        assert result.source_file.file_extension == ".xls"
        assert result.duplicate is False


def test_stored_upload_falls_back_to_copy_across_filesystems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker mounts may make staging and durable storage separate filesystems."""

    content = make_xlsx_bytes()
    temp_root = tmp_path / "temp"
    storage_root = tmp_path / "source"
    staged = stage_upload(BytesIO(content), "valuation.xlsx", temp_root, 1024 * 1024)
    original_replace = Path.replace

    def cross_device_replace(path: Path, target: str | Path) -> Path:
        if path == staged.path:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", cross_device_replace)

    object_name, stored_path = store_staged_upload(staged, storage_root)

    assert stored_path.name == object_name
    assert stored_path.read_bytes() == content
    assert not staged.path.exists()


@pytest.mark.parametrize(
    ("filename", "content", "error_code"),
    [
        ("bad.txt", b"text", "unsupported_extension"),
        ("fake.xlsx", b"PK\x03\x04not-a-workbook", "invalid_file_signature"),
        ("fake.xls", b"not-an-ole-file", "invalid_file_signature"),
    ],
)
def test_invalid_extension_and_disguised_files_are_rejected(
    app_and_engine: tuple[object, object],
    filename: str,
    content: bytes,
    error_code: str,
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)

        with pytest.raises(ImportService.InvalidFile, match=error_code):
            service.receive_upload(batch.id, filename, BytesIO(content), actor.id)

        assert list(Path(app.state.settings.upload_temp_dir).glob("*")) == []


def test_upload_size_limit_is_enforced(app_and_engine: tuple[object, object]) -> None:
    app, engine = app_and_engine
    app.state.settings = replace(app.state.settings, max_upload_bytes=16)
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)

        with pytest.raises(ImportService.FileTooLarge):
            service.receive_upload(
                batch.id, "large.xlsx", BytesIO(make_xlsx_bytes(b"x" * 100)), actor.id
            )


def test_xlsx_member_count_limit_is_enforced(
    app_and_engine: tuple[object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, engine = app_and_engine
    monkeypatch.setattr("app.imports.storage.MAX_XLSX_MEMBERS", 2)
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)

        with pytest.raises(InvalidFileError):
            service.receive_upload(
                batch.id, "too-many-members.xlsx", BytesIO(make_xlsx_bytes()), actor.id
            )


def test_xlsx_uncompressed_size_limit_is_enforced(
    app_and_engine: tuple[object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, engine = app_and_engine
    monkeypatch.setattr("app.imports.storage.MAX_XLSX_UNCOMPRESSED_BYTES", 32)
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)

        with pytest.raises(InvalidFileError):
            service.receive_upload(
                batch.id,
                "too-large-uncompressed.xlsx",
                BytesIO(make_xlsx_bytes(b"x" * 100)),
                actor.id,
            )


def test_complete_batch_is_idempotent_for_queued_and_completed_batches(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        service.receive_upload(
            batch.id, "valuation.xlsx", BytesIO(make_xlsx_bytes()), actor.id
        )

        first_batch, first_job = service.complete_batch(batch.id, actor.id)
        second_batch, second_job = service.complete_batch(batch.id, actor.id)

        assert first_batch.status == "queued"
        assert second_batch.status == "queued"
        assert second_job.id == first_job.id

        first_batch.status = "completed"
        first_job.status = "succeeded"
        session.flush()
        completed_batch, completed_job = service.complete_batch(batch.id, actor.id)
        session.commit()

        assert completed_batch.status == "completed"
        assert completed_job.id == first_job.id


def test_complete_batch_does_not_reset_failed_batch(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        service.receive_upload(
            batch.id, "valuation.xlsx", BytesIO(make_xlsx_bytes()), actor.id
        )
        _, job = service.complete_batch(batch.id, actor.id)
        batch.status = "failed"
        session.commit()

        with pytest.raises(ValueError, match="batch_failed"):
            service.complete_batch(batch.id, actor.id)

        session.rollback()
        refreshed = session.get(type(batch), batch.id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert session.get(type(job), job.id).status == "pending"


def test_storage_path_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    with pytest.raises(UnsafeStoragePathError):
        resolve_in_root(root, "../escape.xlsx")


def test_production_requires_database_and_storage_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("UPLOAD_TEMP_DIR", raising=False)
    monkeypatch.delenv("SOURCE_STORAGE_DIR", raising=False)

    with pytest.raises(
        ValueError,
        match="DATABASE_URL, UPLOAD_TEMP_DIR, SOURCE_STORAGE_DIR required",
    ):
        get_settings()
