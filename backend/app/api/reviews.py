"""Review and valuation publication routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_db, require_roles
from app.auth.fund_access import require_fund_access, visible_fund_ids
from app.db.base import UserRole, ValidationLevel, ValuationStatus
from app.db.models import (
    Fund,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
    ValidationResult,
    ValuationVersion,
)
from app.imports.access import batch_access_clause, source_file_access_clause
from app.publishing import (
    PublishingService,
    PublishingServiceError,
    PublishingStateError,
    PublishingValidationError,
)

router = APIRouter(prefix="/api/v1", tags=["reviews"])
DatabaseSession = Annotated[Session, Depends(get_db)]
ReviewOperator = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]


class ReviewDecision(BaseModel):
    allow_publish: bool
    note: str = Field(min_length=1, max_length=2000)


class PublishRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    confirm_warnings: bool = False


class BatchPublishRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


def _error(exc: PublishingServiceError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/reviews")
def list_reviews(
    context: ReviewOperator,
    session: DatabaseSession,
    version_status: ValuationStatus = Query(  # noqa: B008
        default=ValuationStatus.PENDING_REVIEW, alias="status"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    base = (
        select(ValuationVersion, Fund.standard_name)
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(ValuationVersion.status == version_status)
        .order_by(ValuationVersion.valuation_date, ValuationVersion.id)
    )
    allowed_ids = visible_fund_ids(session, context.user)
    count_statement = select(func.count(ValuationVersion.id)).where(
        ValuationVersion.status == version_status
    )
    if allowed_ids is not None:
        base = base.where(ValuationVersion.fund_id.in_(allowed_ids))
        count_statement = count_statement.where(
            ValuationVersion.fund_id.in_(allowed_ids)
        )
    count = session.scalar(count_statement) or 0
    rows = session.execute(base.offset((page - 1) * page_size).limit(page_size)).all()
    version_ids = [version.id for version, _ in rows]
    findings = (
        session.scalars(
            select(ValidationResult).where(
                ValidationResult.valuation_version_id.in_(version_ids)
            )
        ).all()
        if version_ids
        else []
    )
    counts: dict[int, dict[str, int]] = {}
    for finding in findings:
        current = counts.setdefault(
            finding.valuation_version_id,
            {"critical": 0, "warning": 0, "ignored": 0},
        )
        if finding.ignored:
            current["ignored"] += 1
            continue
        if finding.level == ValidationLevel.CRITICAL:
            current["critical"] += 1
        elif finding.level == ValidationLevel.WARNING:
            current["warning"] += 1
    source_ids = [
        version.source_file_id for version, _ in rows if version.source_file_id
    ]
    sources = (
        {
            source.id: source
            for source in session.scalars(
                select(SourceFile).where(
                    SourceFile.id.in_(source_ids),
                    source_file_access_clause(session, context.user),
                )
            )
        }
        if source_ids
        else {}
    )
    batch_links = (
        session.execute(
            select(ImportBatchFile.source_file_id, ImportBatchFile.batch_id)
            .join(ImportBatch, ImportBatch.id == ImportBatchFile.batch_id)
            .where(
                ImportBatchFile.source_file_id.in_(source_ids),
                batch_access_clause(session, context.user),
            )
            .order_by(ImportBatchFile.id)
        ).all()
        if source_ids
        else []
    )
    batch_by_source = {source_id: batch_id for source_id, batch_id in batch_links}
    findings_by_version: dict[int, list[ValidationResult]] = {}
    for finding in findings:
        findings_by_version.setdefault(finding.valuation_version_id, []).append(finding)
    data = []
    for version, fund_name in rows:
        source = sources.get(version.source_file_id)
        version_counts = counts.get(version.id, {})
        data.append(
            {
                "id": version.id,
                "fund_id": version.fund_id,
                "fund_name": fund_name,
                "valuation_date": version.valuation_date.isoformat(),
                "version_no": version.version_no,
                "status": version.status,
                "critical_count": version_counts.get("critical", 0),
                "warning_count": version_counts.get("warning", 0),
                "ignored_count": version_counts.get("ignored", 0),
                "source_file_id": source.id if source else None,
                "source_filename": source.original_filename if source else None,
                "source_file_hash": source.file_hash if source else None,
                "source_file_size": source.file_size if source else None,
                "import_batch_id": batch_by_source.get(source.id) if source else None,
                "findings": [
                    _validation_data(item)
                    for item in findings_by_version.get(version.id, [])
                ],
            }
        )
    return {
        "data": data,
        "meta": {"page": page, "page_size": page_size, "total": count},
    }


@router.post("/reviews/{version_id}/acknowledge")
def acknowledge_review(
    version_id: int,
    payload: ReviewDecision,
    context: ReviewOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    _require_version_access(session, context, version_id)
    try:
        result = PublishingService(session).acknowledge_review(
            version_id,
            allow_publish=payload.allow_publish,
            actor_user_id=context.user.id,
            note=payload.note,
        )
        session.commit()
    except PublishingServiceError as exc:
        session.rollback()
        raise _error(exc) from exc
    return {"data": {"version_id": result.version_id, "status": result.status}}


@router.post("/valuations/{version_id}/publish")
def publish_version(
    version_id: int,
    payload: PublishRequest,
    context: ReviewOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    _require_version_access(session, context, version_id)
    try:
        result = PublishingService(session).publish_version(
            version_id,
            actor_user_id=context.user.id,
            actor_label=context.user.username,
            reason=payload.reason,
            confirm_warnings=payload.confirm_warnings,
            ignore_validations=True,
        )
        session.commit()
    except (PublishingValidationError, PublishingStateError) as exc:
        session.rollback()
        raise _error(exc) from exc
    except PublishingServiceError as exc:
        session.rollback()
        raise _error(exc) from exc
    return {
        "data": {
            "version_id": result.version_id,
            "fund_id": result.fund_id,
            "valuation_date": result.valuation_date.isoformat(),
            "superseded_version_ids": list(result.superseded_version_ids),
            "analysis_run_id": result.analysis_run_id,
            "validation_ignored_count": result.validation_ignored_count,
        }
    }


@router.post("/reviews/batch-publish")
def batch_publish(
    payload: BatchPublishRequest,
    context: ReviewOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    """Publish every version currently waiting in the publishable queue."""

    try:
        result = PublishingService(session).publish_all_publishable(
            allowed_fund_ids=visible_fund_ids(session, context.user),
            actor_user_id=context.user.id,
            actor_label=context.user.username,
            reason=payload.reason,
        )
        session.commit()
    except PublishingServiceError as exc:
        session.rollback()
        raise _error(exc) from exc
    return {
        "data": {
            "requested": result.requested,
            "published": result.published,
            "failed": list(result.failed),
            "ignored_findings": result.ignored_findings,
        }
    }


@router.post("/valuations/{version_id}/reject")
def reject_version(
    version_id: int,
    payload: ReasonRequest,
    context: ReviewOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    _require_version_access(session, context, version_id)
    try:
        result = PublishingService(session).reject_version(
            version_id, actor_user_id=context.user.id, reason=payload.reason
        )
        session.commit()
    except PublishingServiceError as exc:
        session.rollback()
        raise _error(exc) from exc
    return {"data": {"version_id": result.version_id, "status": result.status}}


@router.post("/valuations/{version_id}/revoke")
def revoke_version(
    version_id: int,
    payload: ReasonRequest,
    context: ReviewOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    _require_version_access(session, context, version_id)
    try:
        result = PublishingService(session).revoke_version(
            version_id, actor_user_id=context.user.id, reason=payload.reason
        )
        session.commit()
    except PublishingServiceError as exc:
        session.rollback()
        raise _error(exc) from exc
    return {
        "data": {
            "version_id": result.version_id,
            "status": "revoked",
            "analysis_run_id": result.analysis_run_id,
        }
    }


@router.post("/valuations/{version_id}/restore")
def restore_version(
    version_id: int,
    payload: ReasonRequest,
    context: ReviewOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    _require_version_access(session, context, version_id)
    try:
        result = PublishingService(session).restore_version(
            version_id,
            actor_user_id=context.user.id,
            actor_label=context.user.username,
            reason=payload.reason,
        )
        session.commit()
    except PublishingServiceError as exc:
        session.rollback()
        raise _error(exc) from exc
    return {
        "data": {
            "version_id": result.version_id,
            "status": "published",
            "superseded_version_ids": list(result.superseded_version_ids),
            "analysis_run_id": result.analysis_run_id,
        }
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


def _require_version_access(
    session: Session, context: AuthContext, version_id: int
) -> None:
    if visible_fund_ids(session, context.user) is None:
        return
    version = session.get(ValuationVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Valuation version not found")
    require_fund_access(session, context.user, version.fund_id)
