"""Database-backed import job claiming and state transitions."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import and_, event, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.analytics.service import AnalysisProcessResult, process_analysis_run
from app.config import Settings
from app.db.base import AnalysisRunStatus, AuditResult, ImportBatchStatus, JobStatus
from app.db.models import AnalysisRun, AuditLog, BackgroundJob, ImportBatch
from app.mail import MailService, MailSettings, MailSyncResult
from app.system.settings import effective_mail_username

from .processor import BatchProcessResult, process_import_batch

if TYPE_CHECKING:
    from app.benchmark.sync import SyncResult

JOB_LEASE_DURATION = timedelta(minutes=15)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def _clear_job_transition_marker(session: Session) -> None:
    session.info.pop("job_transition_pending", None)


def claim_next_job(
    session: Session, *, now: datetime | None = None
) -> BackgroundJob | None:
    """Claim the oldest eligible job in a short, independent transaction.

    Claiming must not commit unrelated work held by the caller's session.  The
    returned object is merged back into that session only as a read-only lease
    snapshot; worker writes still commit through the caller after lease checks.
    """

    if session.new or session.dirty or session.deleted:
        raise RuntimeError("job_claim_requires_clean_session")

    current_time = now or datetime.now(UTC)
    expired_before = current_time - JOB_LEASE_DURATION
    bind = session.get_bind(mapper=BackgroundJob)
    claim_session = Session(bind=bind, expire_on_commit=False)
    try:
        statement = (
            select(BackgroundJob)
            .where(
                or_(
                    BackgroundJob.status == JobStatus.PENDING,
                    and_(
                        BackgroundJob.status == JobStatus.RETRY_DUE,
                        BackgroundJob.next_retry_at <= current_time,
                    ),
                    and_(
                        BackgroundJob.status == JobStatus.RUNNING,
                        BackgroundJob.locked_at.is_not(None),
                        BackgroundJob.locked_at < expired_before,
                    ),
                )
            )
            .order_by(BackgroundJob.id)
            .limit(1)
        )
        if bind.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)

        job = claim_session.scalar(statement)
        if job is None:
            return None
        if job.status == JobStatus.RUNNING and job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
            job.error_code = "max_attempts_exceeded"
            job.finished_at = current_time
            job.locked_at = None
            job.lease_token = None
            _mark_resource_failed(
                claim_session,
                job,
                error_code="max_attempts_exceeded",
                now=current_time,
            )
            claim_session.commit()
            return None

        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.lease_token = secrets.token_hex(32)
        job.locked_at = current_time
        if job.started_at is None:
            job.started_at = current_time
        job.error_code = None
        job.next_retry_at = None
        claim_session.commit()
        claim_session.expunge(job)
    finally:
        claim_session.close()

    return session.merge(job, load=False)


def finish_job(
    session: Session,
    job: BackgroundJob,
    *,
    lease_token: str | None = None,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(UTC)
    token = job.lease_token if lease_token is None else lease_token
    if token is None:
        return False
    result = cast(
        CursorResult[Any],
        session.execute(
            update(BackgroundJob)
            .execution_options(synchronize_session=False)
            .where(
                BackgroundJob.id == job.id,
                BackgroundJob.status == JobStatus.RUNNING,
                BackgroundJob.lease_token == token,
            )
            .values(
                status=JobStatus.SUCCEEDED,
                finished_at=current_time,
                locked_at=None,
                lease_token=None,
                error_code=None,
                next_retry_at=None,
            )
        ),
    )
    if result.rowcount != 1:
        return False
    job.status = JobStatus.SUCCEEDED
    job.finished_at = current_time
    job.locked_at = None
    job.lease_token = None
    job.error_code = None
    job.next_retry_at = None
    session.flush()
    session.info["job_transition_pending"] = True
    return True


def fail_job(
    session: Session,
    job: BackgroundJob,
    error_code: str,
    *,
    retryable: bool,
    lease_token: str | None = None,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(UTC)
    token = job.lease_token if lease_token is None else lease_token
    if token is None:
        return False
    if retryable and job.attempts < job.max_attempts:
        delay_seconds = 60 * (2 ** max(job.attempts - 1, 0))
        values = {
            "status": JobStatus.RETRY_DUE,
            "next_retry_at": current_time + timedelta(seconds=delay_seconds),
            "finished_at": None,
        }
    else:
        values = {
            "status": JobStatus.FAILED,
            "next_retry_at": None,
            "finished_at": current_time,
        }
    result = cast(
        CursorResult[Any],
        session.execute(
            update(BackgroundJob)
            .execution_options(synchronize_session=False)
            .where(
                BackgroundJob.id == job.id,
                BackgroundJob.status == JobStatus.RUNNING,
                BackgroundJob.lease_token == token,
            )
            .values(
                **values,
                error_code=error_code,
                locked_at=None,
                lease_token=None,
            )
        ),
    )
    if result.rowcount != 1:
        return False
    job.status = values["status"]
    job.next_retry_at = values["next_retry_at"]
    job.finished_at = values["finished_at"]
    job.error_code = error_code
    job.locked_at = None
    job.lease_token = None
    session.flush()
    session.info["job_transition_pending"] = True
    return True


def process_next_job(
    session: Session, settings: Settings
) -> (
    tuple[
        BackgroundJob,
        BatchProcessResult | AnalysisProcessResult | MailSyncResult | SyncResult | None,
    ]
    | None
):
    """Claim and execute one database-backed job."""

    job = claim_next_job(session)
    if job is None:
        return None
    lease_token = job.lease_token
    resource_id: int | None = None
    try:
        if job.job_type == "process_import_batch":
            resource_id = int(job.resource_id)
            result = process_import_batch(session, resource_id, settings)
        elif job.job_type == "process_analysis_run":
            resource_id = int(job.resource_id)
            result = process_analysis_run(session, resource_id)
        elif job.job_type == "benchmark_sync":
            from app.benchmark.sync import sync_benchmark

            result = sync_benchmark(session, settings)
            if not finish_job(session, job, lease_token=lease_token):
                session.rollback()
                return job, None
            session.commit()
            return job, result
        elif job.job_type == "mail_sync":
            actor_user_id = session.scalar(
                select(AuditLog.actor_user_id)
                .where(
                    AuditLog.resource_type == "mail_sync",
                    AuditLog.resource_id == job.resource_id,
                    AuditLog.action == "mail.sync_started",
                )
                .limit(1)
            )
            mail_settings = MailSettings.from_environment(
                username_override=effective_mail_username(session)
            )
            result = MailService.from_app_settings(
                session, settings, mail_settings
            ).sync(
                actor_user_id,
                run_id=job.resource_id,
                job=job,
            )
            session.commit()
            return job, result
        else:
            raise ValueError("unsupported_job_type")
        if not finish_job(session, job, lease_token=lease_token):
            session.rollback()
            return job, None
        session.commit()
        return job, result
    except ValueError:
        session.rollback()
        refreshed = session.get(BackgroundJob, job.id)
        if refreshed is None:
            raise
        updated = fail_job(
            session,
            refreshed,
            _processing_error_code(job),
            retryable=False,
            lease_token=lease_token,
        )
        if updated:
            _mark_resource_failed(
                session,
                refreshed,
                error_code=_processing_error_code(job),
                now=datetime.now(UTC),
            )
        session.commit()
        return refreshed, None
    except Exception:
        session.rollback()
        refreshed = session.get(BackgroundJob, job.id)
        if refreshed is None:
            raise
        updated = fail_job(
            session,
            refreshed,
            _processing_error_code(job),
            retryable=True,
            lease_token=lease_token,
        )
        if updated and refreshed.status == JobStatus.FAILED:
            _mark_resource_failed(
                session,
                refreshed,
                error_code=_processing_error_code(job),
                now=datetime.now(UTC),
            )
        session.commit()
        return refreshed, None


def _processing_error_code(job: BackgroundJob) -> str:
    if job.job_type == "mail_sync":
        return "mail_sync_failed"
    if job.job_type == "benchmark_sync":
        return "benchmark_sync_failed"
    return (
        "analysis_processing_failed"
        if job.job_type == "process_analysis_run"
        else "batch_processing_failed"
    )


def _mark_resource_failed(
    session: Session,
    job: BackgroundJob,
    *,
    error_code: str,
    now: datetime,
) -> None:
    try:
        resource_id = int(job.resource_id)
    except ValueError:
        return
    if job.job_type == "process_import_batch":
        batch = session.get(ImportBatch, resource_id)
        if batch is not None:
            batch.status = ImportBatchStatus.FAILED
            batch.ended_at = now
        return
    if job.job_type != "process_analysis_run":
        return
    run = session.get(AnalysisRun, resource_id)
    if run is None:
        return
    run.status = AnalysisRunStatus.FAILED
    run.error_message = error_code
    run.ended_at = now
    session.add(
        AuditLog(
            action="analysis.failed",
            resource_type="analysis_run",
            resource_id=str(run.id),
            summary={"error_code": error_code},
            result=AuditResult.FAILURE,
        )
    )
