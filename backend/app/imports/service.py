"""Import batch, idempotent source-file, and background-job orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import AuditResult, ImportBatchStatus, JobStatus, SourceType
from app.db.models import (
    AuditLog,
    BackgroundJob,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
)

from .access import ImportAccessError, actor_fund_ids, authorized_workbook_fund
from .storage import (
    FileTooLargeError,
    InvalidFileError,
    discard_staged_upload,
    forget_stored_object,
    remove_stored_object,
    stage_upload,
    store_staged_upload,
    track_stored_object,
)


@dataclass(frozen=True, slots=True)
class UploadResult:
    source_file: SourceFile
    duplicate: bool


class ImportService:
    """Deep module for raw file receipt and import batch lifecycle."""

    InvalidFile = InvalidFileError
    FileTooLarge = FileTooLargeError

    def __init__(
        self,
        session: Session,
        *,
        temp_dir: Path,
        storage_dir: Path,
        max_file_size: int,
    ) -> None:
        self.session = session
        self.temp_dir = temp_dir.resolve()
        self.storage_dir = storage_dir.resolve()
        self.max_file_size = max_file_size

    @classmethod
    def from_settings(cls, session: Session, settings: Settings) -> ImportService:
        return cls(
            session,
            temp_dir=Path(settings.upload_temp_dir),
            storage_dir=Path(settings.source_storage_dir),
            max_file_size=settings.max_upload_bytes,
        )

    def create_batch(
        self,
        source_type: SourceType,
        actor_user_id: int | None,
        source_message_id: int | None = None,
    ) -> ImportBatch:
        batch = ImportBatch(
            source_type=source_type,
            created_by_user_id=actor_user_id,
            source_message_id=source_message_id,
            file_count=0,
            status=ImportBatchStatus.CREATED,
        )
        self.session.add(batch)
        self.session.flush()
        return batch

    def receive_upload(
        self,
        batch_id: int,
        original_filename: str,
        stream: BinaryIO,
        actor_user_id: int | None,
    ) -> UploadResult:
        batch = self._get_batch(batch_id, lock=True)
        if batch.status != ImportBatchStatus.CREATED:
            self._record_audit(
                action="import.upload_failed",
                batch_id=batch.id,
                actor_user_id=actor_user_id,
                result=AuditResult.FAILURE,
                summary={"error_code": "batch_not_open"},
            )
            self.session.flush()
            raise ValueError("batch_not_open")

        try:
            staged = stage_upload(
                stream,
                original_filename,
                self.temp_dir,
                self.max_file_size,
            )
        except InvalidFileError as exc:
            self._record_audit(
                action="import.upload_failed",
                batch_id=batch.id,
                actor_user_id=actor_user_id,
                result=AuditResult.FAILURE,
                summary={"error_code": exc.code},
            )
            self.session.flush()
            raise

        recognized_fund_id = None
        allowed_ids = actor_fund_ids(self.session, actor_user_id)
        if allowed_ids is not None:
            try:
                recognized_fund_id = authorized_workbook_fund(
                    self.session, staged.path, allowed_ids
                )
            except ImportAccessError:
                discard_staged_upload(staged, self.temp_dir)
                raise

        existing_file = self.session.scalar(
            select(SourceFile).where(SourceFile.file_hash == staged.file_hash)
        )
        if existing_file is not None:
            discard_staged_upload(staged, self.temp_dir)
            self._authorize_existing_file(
                existing_file, allowed_ids, recognized_fund_id
            )
            self._link_file(batch, existing_file, duplicate=True)
            self._record_audit(
                action="import.duplicate_file",
                batch_id=batch.id,
                actor_user_id=actor_user_id,
                summary={"source_file_id": existing_file.id},
            )
            self.session.flush()
            return UploadResult(source_file=existing_file, duplicate=True)

        object_name, stored_path = store_staged_upload(staged, self.storage_dir)
        track_stored_object(self.session, object_name, self.storage_dir)
        source_file = SourceFile(
            original_filename=staged.original_filename,
            recognized_fund_id=recognized_fund_id,
            file_hash=staged.file_hash,
            file_size=staged.file_size,
            file_extension=staged.extension,
            source_type=batch.source_type,
            object_name=object_name,
        )
        try:
            with self.session.begin_nested():
                self.session.add(source_file)
                self.session.flush()
        except IntegrityError:
            existing_file = self.session.scalar(
                select(SourceFile).where(SourceFile.file_hash == staged.file_hash)
            )
            if existing_file is None:
                remove_stored_object(stored_path, self.storage_dir)
                forget_stored_object(self.session, object_name, self.storage_dir)
                raise
            remove_stored_object(stored_path, self.storage_dir)
            forget_stored_object(self.session, object_name, self.storage_dir)
            self._authorize_existing_file(
                existing_file, allowed_ids, recognized_fund_id
            )
            self._link_file(batch, existing_file, duplicate=True)
            self._record_audit(
                action="import.duplicate_file",
                batch_id=batch.id,
                actor_user_id=actor_user_id,
                summary={"source_file_id": existing_file.id},
            )
            self.session.flush()
            return UploadResult(source_file=existing_file, duplicate=True)
        except Exception:
            remove_stored_object(stored_path, self.storage_dir)
            forget_stored_object(self.session, object_name, self.storage_dir)
            raise
        self._link_file(batch, source_file, duplicate=False)
        self._record_audit(
            action="import.upload",
            batch_id=batch.id,
            actor_user_id=actor_user_id,
            summary={"source_file_id": source_file.id},
        )
        self.session.flush()
        return UploadResult(source_file=source_file, duplicate=False)

    def _authorize_existing_file(
        self,
        source_file: SourceFile,
        allowed_ids: frozenset[int] | None,
        recognized_fund_id: int | None,
    ) -> None:
        if allowed_ids is None:
            return
        from app.db.models import ValuationVersion

        version_funds = set(
            self.session.scalars(
                select(ValuationVersion.fund_id).where(
                    ValuationVersion.source_file_id == source_file.id
                )
            )
        )
        if version_funds - allowed_ids or (
            source_file.recognized_fund_id is not None
            and source_file.recognized_fund_id not in allowed_ids
        ):
            raise ImportAccessError("product_access_denied")
        source_file.recognized_fund_id = recognized_fund_id

    def complete_batch(
        self, batch_id: int, actor_user_id: int | None
    ) -> tuple[ImportBatch, BackgroundJob]:
        batch = self._get_batch(batch_id, lock=True)
        status = ImportBatchStatus(batch.status)
        if status == ImportBatchStatus.FAILED:
            self._record_audit(
                action="import.complete_failed",
                batch_id=batch.id,
                actor_user_id=actor_user_id,
                result=AuditResult.FAILURE,
                summary={"error_code": "batch_failed"},
            )
            self.session.flush()
            raise ValueError("batch_failed")

        job = self.session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "process_import_batch",
                BackgroundJob.resource_id == str(batch.id),
            )
        )
        if status in {
            ImportBatchStatus.QUEUED,
            ImportBatchStatus.PROCESSING,
            ImportBatchStatus.COMPLETED,
        }:
            if job is None:
                raise ValueError("batch_job_missing")
            return batch, job

        if status != ImportBatchStatus.CREATED:
            raise ValueError("invalid_batch_status")
        if batch.file_count == 0:
            self._record_audit(
                action="import.complete_failed",
                batch_id=batch.id,
                actor_user_id=actor_user_id,
                result=AuditResult.FAILURE,
                summary={"error_code": "empty_batch"},
            )
            self.session.flush()
            raise ValueError("empty_batch")

        if job is None:
            job = BackgroundJob(
                job_type="process_import_batch",
                resource_id=str(batch.id),
                status=JobStatus.PENDING,
            )
            self.session.add(job)
        batch.status = ImportBatchStatus.QUEUED
        self._record_audit(
            action="import.complete_batch",
            batch_id=batch.id,
            actor_user_id=actor_user_id,
        )
        self.session.flush()
        return batch, job

    def get_batch(self, batch_id: int) -> ImportBatch:
        return self._get_batch(batch_id)

    def _get_batch(self, batch_id: int, *, lock: bool = False) -> ImportBatch:
        statement = select(ImportBatch).where(ImportBatch.id == batch_id)
        if (
            lock
            and self.session.bind is not None
            and self.session.bind.dialect.name == "postgresql"
        ):
            statement = statement.with_for_update()
        batch = self.session.scalar(statement)
        if batch is None:
            raise LookupError(batch_id)
        return batch

    def _link_file(
        self, batch: ImportBatch, source_file: SourceFile, *, duplicate: bool
    ) -> ImportBatchFile:
        link = self.session.scalar(
            select(ImportBatchFile).where(
                ImportBatchFile.batch_id == batch.id,
                ImportBatchFile.source_file_id == source_file.id,
            )
        )
        if link is None:
            link = ImportBatchFile(
                batch_id=batch.id,
                source_file_id=source_file.id,
                duplicate=duplicate,
            )
            self.session.add(link)
            batch.file_count += 1
        return link

    def _record_audit(
        self,
        *,
        action: str,
        batch_id: int,
        actor_user_id: int | None,
        result: AuditResult = AuditResult.SUCCESS,
        summary: dict[str, object] | None = None,
    ) -> None:
        self.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                resource_type="import_batch",
                resource_id=str(batch_id),
                summary=summary,
                result=result,
            )
        )
