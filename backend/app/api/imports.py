"""Authenticated raw file intake and import batch routes."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.auth.dependencies import AuthContext, get_db, require_roles
from app.db.base import AuditResult, ImportBatchStatus, JobStatus, SourceType, UserRole
from app.db.models import (
    AuditLog,
    BackgroundJob,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
    ValidationResult,
    ValuationVersion,
)
from app.imports.access import (
    ImportAccessError,
    batch_access_clause,
    require_batch_access,
)
from app.imports.http_upload import bounded_upload
from app.imports.service import ImportService
from app.imports.storage import resolve_in_root

router = APIRouter(prefix="/api/v1/imports", tags=["imports"])

DatabaseSession = Annotated[Session, Depends(get_db)]
ImportOperator = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]


class CreateBatchRequest(BaseModel):
    source_type: SourceType = SourceType.UPLOAD


def _service(request: Request, session: Session) -> ImportService:
    return ImportService.from_settings(session, request.app.state.settings)


async def _authorized_upload(
    request: Request, context: ImportOperator
) -> AsyncIterator[UploadFile]:
    async with bounded_upload(
        request, max_file_bytes=request.app.state.settings.max_upload_bytes
    ) as upload:
        yield upload


@router.post("", status_code=status.HTTP_201_CREATED)
def create_batch(
    payload: CreateBatchRequest,
    request: Request,
    context: ImportOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    batch = _service(request, session).create_batch(
        payload.source_type, context.user.id
    )
    session.commit()
    return {"data": _batch_data(batch)}


@router.get("")
def list_batches(
    context: ImportOperator,
    session: DatabaseSession,
    source_type: SourceType | None = None,
    status_filter: ImportBatchStatus | None = Query(  # noqa: B008
        default=None, alias="status"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    """List import batches for the operations page."""

    statement = select(ImportBatch).order_by(
        ImportBatch.created_at.desc(), ImportBatch.id.desc()
    )
    access = batch_access_clause(session, context.user)
    statement = statement.where(access)
    count_statement = select(func.count(ImportBatch.id)).where(access)
    if source_type is not None:
        statement = statement.where(ImportBatch.source_type == source_type)
        count_statement = count_statement.where(ImportBatch.source_type == source_type)
    if status_filter is not None:
        statement = statement.where(ImportBatch.status == status_filter)
        count_statement = count_statement.where(ImportBatch.status == status_filter)
    offset = (page - 1) * page_size
    total = session.scalar(count_statement) or 0
    page_batches = list(session.scalars(statement.offset(offset).limit(page_size)))
    batch_ids = [batch.id for batch in page_batches]
    jobs = (
        session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "process_import_batch",
                BackgroundJob.resource_id.in_([str(item) for item in batch_ids]),
            )
        ).all()
        if batch_ids
        else []
    )
    jobs_by_batch = {int(job.resource_id): job for job in jobs}
    return {
        "data": [
            {
                **_batch_data(batch),
                "job": _job_data(jobs_by_batch.get(batch.id)),
            }
            for batch in page_batches
        ],
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


@router.post(
    "/{batch_id}/files",
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
def upload_file(
    batch_id: int,
    request: Request,
    context: ImportOperator,
    session: DatabaseSession,
    file: Annotated[UploadFile, Depends(_authorized_upload)],
) -> dict[str, object]:
    require_batch_access(session, context.user, batch_id, write=True)
    service = _service(request, session)
    try:
        result = service.receive_upload(
            batch_id,
            file.filename or "upload",
            file.file,
            context.user.id,
        )
        session.commit()
    except ImportAccessError as exc:
        session.rollback()
        raise HTTPException(
            status_code=403, detail="File product is not authorized"
        ) from exc
    except ImportService.FileTooLarge as exc:
        session.commit()
        raise HTTPException(status_code=413, detail=exc.code) from exc
    except ImportService.InvalidFile as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Import batch not found") from exc
    except ValueError as exc:
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "data": {
            "id": result.source_file.id,
            "original_filename": result.source_file.original_filename,
            "file_hash": result.source_file.file_hash,
            "file_size": result.source_file.file_size,
            "duplicate": result.duplicate,
        }
    }


@router.post("/{batch_id}/complete")
def complete_batch(
    batch_id: int,
    request: Request,
    context: ImportOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_batch_access(session, context.user, batch_id, write=True)
    try:
        batch, job = _service(request, session).complete_batch(
            batch_id, context.user.id
        )
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="Import batch not found") from exc
    except ValueError as exc:
        session.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"data": {**_batch_data(batch), "job": _job_data(job)}}


@router.get("/{batch_id}")
def get_batch(
    batch_id: int,
    request: Request,
    context: ImportOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_batch_access(session, context.user, batch_id, write=False)
    service = _service(request, session)
    try:
        batch = service.get_batch(batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Import batch not found") from exc
    links = session.scalars(
        select(ImportBatchFile).where(ImportBatchFile.batch_id == batch.id)
    ).all()
    job = session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.job_type == "process_import_batch",
            BackgroundJob.resource_id == str(batch.id),
        )
    )
    return {
        "data": {
            **_batch_data(batch),
            "files": [
                {
                    "id": link.source_file.id,
                    "original_filename": link.source_file.original_filename,
                    "file_hash": link.source_file.file_hash,
                    "file_size": link.source_file.file_size,
                    "duplicate": link.duplicate,
                }
                for link in links
            ],
            "job": _job_data(job) if job else None,
        }
    }


@router.post("/{batch_id}/retry")
def retry_batch(
    batch_id: int,
    context: ImportOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    """Reset one terminal technical import failure for a manual retry."""
    require_batch_access(session, context.user, batch_id, write=True)

    batch = session.get(ImportBatch, batch_id)
    job = session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.job_type == "process_import_batch",
            BackgroundJob.resource_id == str(batch_id),
        )
    )
    if batch is None or job is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch.status != ImportBatchStatus.FAILED or job.status != JobStatus.FAILED:
        raise HTTPException(status_code=409, detail="Import batch is not retryable")
    if job.error_code not in {"batch_processing_failed", "max_attempts_exceeded"}:
        raise HTTPException(status_code=409, detail="Import batch is not retryable")

    job.status = JobStatus.PENDING
    job.attempts = 0
    job.locked_at = None
    job.lease_token = None
    job.started_at = None
    job.finished_at = None
    job.error_code = None
    job.next_retry_at = None
    batch.status = ImportBatchStatus.QUEUED
    batch.started_at = None
    batch.ended_at = None
    session.add(
        AuditLog(
            actor_user_id=context.user.id,
            action="import.retry",
            resource_type="import_batch",
            resource_id=str(batch.id),
            summary={"job_id": job.id},
            result=AuditResult.SUCCESS,
        )
    )
    session.commit()
    return {"data": {**_batch_data(batch), "job": _job_data(job)}}


@router.get("/{batch_id}/validations")
def batch_validations(
    batch_id: int,
    context: ImportOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    """Return validation findings grouped by versions created by a batch."""
    require_batch_access(session, context.user, batch_id, write=False)

    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    source_ids = select(ImportBatchFile.source_file_id).where(
        ImportBatchFile.batch_id == batch.id
    )
    versions = list(
        session.scalars(
            select(ValuationVersion)
            .where(ValuationVersion.source_file_id.in_(source_ids))
            .order_by(ValuationVersion.valuation_date, ValuationVersion.version_no)
        )
    )
    version_ids = [version.id for version in versions]
    findings = (
        session.scalars(
            select(ValidationResult)
            .where(ValidationResult.valuation_version_id.in_(version_ids))
            .order_by(ValidationResult.level, ValidationResult.id)
        ).all()
        if version_ids
        else []
    )
    findings_by_version: dict[int, list[ValidationResult]] = {}
    for finding in findings:
        findings_by_version.setdefault(finding.valuation_version_id, []).append(finding)
    return {
        "data": [
            {
                "version_id": version.id,
                "fund_id": version.fund_id,
                "valuation_date": version.valuation_date.isoformat(),
                "status": version.status,
                "findings": [
                    _validation_data(item)
                    for item in findings_by_version.get(version.id, [])
                ],
            }
            for version in versions
        ],
        "meta": {"version_count": len(versions)},
    }


@router.get("/{batch_id}/source/{source_file_id}")
def download_source(
    batch_id: int,
    source_file_id: int,
    request: Request,
    context: ImportOperator,
    session: DatabaseSession,
) -> FileResponse:
    """Download an original file only through an audited, root-confined path."""
    require_batch_access(session, context.user, batch_id, write=False)

    link = session.scalar(
        select(ImportBatchFile).where(
            ImportBatchFile.batch_id == batch_id,
            ImportBatchFile.source_file_id == source_file_id,
        )
    )
    source_file = session.get(SourceFile, source_file_id)
    if link is None or source_file is None:
        raise HTTPException(status_code=404, detail="Source file not found")
    try:
        path = resolve_in_root(
            Path(request.app.state.settings.source_storage_dir), source_file.object_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Source file not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Source file not found")
    session.add(
        AuditLog(
            actor_user_id=context.user.id,
            action="import.source_download",
            resource_type="source_file",
            resource_id=str(source_file.id),
            summary={"batch_id": batch_id, "file_hash": source_file.file_hash},
            result=AuditResult.SUCCESS,
        )
    )
    session.commit()
    media_type = mimetypes.guess_type(source_file.original_filename)[0]
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        filename=source_file.original_filename,
    )


def _batch_data(batch: ImportBatch) -> dict[str, object]:
    return {
        "id": batch.id,
        "source_type": batch.source_type,
        "file_count": batch.file_count,
        "status": batch.status,
        "created_at": batch.created_at,
    }


def _job_data(job: BackgroundJob | None) -> dict[str, object] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "locked_at": job.locked_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error_code": job.error_code,
        "next_retry_at": job.next_retry_at,
        "can_retry": job.status == JobStatus.FAILED,
    }


def _validation_data(finding: ValidationResult) -> dict[str, object]:
    return {
        "rule_code": finding.rule_code,
        "level": finding.level,
        "actual_value": str(finding.actual_value)
        if finding.actual_value is not None
        else None,
        "expected_value": str(finding.expected_value)
        if finding.expected_value is not None
        else None,
        "difference": str(finding.difference)
        if finding.difference is not None
        else None,
        "source_location": finding.source_location,
        "message": finding.message,
        "ignored": finding.ignored,
        "ignored_at": finding.ignored_at.isoformat() if finding.ignored_at else None,
        "ignored_reason": finding.ignored_reason,
    }
