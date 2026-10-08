from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.db.base import JobStatus
from app.db.models import BackgroundJob, ImportBatch
from app.imports.tasks import (
    claim_next_job,
    fail_job,
    finish_job,
    process_next_job,
)


def test_claim_job_and_finish(app_and_engine: tuple[object, object]) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        job = BackgroundJob(job_type="process_import_batch", resource_id="1")
        session.add(job)
        session.commit()

        claimed = claim_next_job(session, now=now)
        assert claimed is not None
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempts == 1
        assert claimed.started_at == now

        finish_job(session, claimed, now=now + timedelta(seconds=10))
        assert claimed.status == JobStatus.SUCCEEDED
        assert claimed.finished_at == now + timedelta(seconds=10)


def test_retryable_failure_uses_limited_backoff(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        job = BackgroundJob(
            job_type="process_import_batch", resource_id="1", max_attempts=2
        )
        session.add(job)
        session.commit()

        claimed = claim_next_job(session, now=now)
        fail_job(session, claimed, "temporary_io", retryable=True, now=now)
        session.commit()
        assert claimed.status == JobStatus.RETRY_DUE
        assert claimed.next_retry_at is not None
        assert claim_next_job(session, now=now) is None

        retry = claim_next_job(session, now=claimed.next_retry_at)
        fail_job(
            session,
            retry,
            "temporary_io",
            retryable=True,
            now=claimed.next_retry_at,
        )
        assert retry.status == JobStatus.FAILED


def test_claim_never_commits_unrelated_background_job_changes(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        session.add(BackgroundJob(job_type="process_import_batch", resource_id="1"))
        session.commit()
        session.get(BackgroundJob, 1).error_code = "local-only"
        with pytest.raises(RuntimeError, match="job_claim_requires_clean_session"):
            claim_next_job(session)
        session.rollback()
        assert session.get(BackgroundJob, 1).error_code is None


def test_claim_is_persisted_before_worker_can_rollback(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(BackgroundJob(job_type="process_import_batch", resource_id="1"))
        session.commit()

        claimed = claim_next_job(session, now=now)
        assert claimed is not None
        lease_token = claimed.lease_token
        session.rollback()

        with Session(engine) as observer:
            persisted = observer.get(BackgroundJob, claimed.id)
            assert persisted is not None
            assert persisted.status == JobStatus.RUNNING
            assert persisted.attempts == 1
            assert persisted.locked_at.replace(tzinfo=UTC) == now
            assert persisted.lease_token == lease_token


def test_expired_running_lease_is_reclaimed_with_a_new_attempt(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(BackgroundJob(job_type="process_import_batch", resource_id="1"))
        session.commit()

        first = claim_next_job(session, now=now)
        assert first is not None
        first_token = first.lease_token

        assert claim_next_job(session, now=now + timedelta(minutes=15)) is None
        second = claim_next_job(session, now=now + timedelta(minutes=15, seconds=1))

        assert second is not None
        assert second.id == first.id
        assert second.status == JobStatus.RUNNING
        assert second.attempts == 2
        assert second.lease_token is not None
        assert second.lease_token != first_token


def test_expired_job_at_attempt_limit_is_terminalized(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        batch = ImportBatch(source_type="upload", file_count=1, status="processing")
        session.add(batch)
        session.flush()
        session.add(
            BackgroundJob(
                job_type="process_import_batch",
                resource_id=str(batch.id),
                attempts=1,
                max_attempts=1,
                status=JobStatus.RUNNING,
                locked_at=now - timedelta(minutes=16),
                lease_token="expired-token",
            )
        )
        session.commit()

        assert claim_next_job(session, now=now) is None
        session.expire_all()
        job = session.scalar(
            __import__("sqlalchemy")
            .select(BackgroundJob)
            .where(BackgroundJob.resource_id == str(batch.id))
        )
        persisted_batch = session.get(ImportBatch, batch.id)
        assert job is not None
        assert persisted_batch is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code == "max_attempts_exceeded"
        assert persisted_batch.status == "failed"


def test_old_lease_cannot_finish_a_reclaimed_job(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as setup:
        setup.add(BackgroundJob(job_type="process_import_batch", resource_id="1"))
        setup.commit()

    with Session(engine) as old_worker:
        first = claim_next_job(old_worker, now=now)
        assert first is not None

        with Session(engine) as new_worker:
            second = claim_next_job(
                new_worker, now=now + timedelta(minutes=15, seconds=1)
            )
            assert second is not None
            assert finish_job(old_worker, first, now=now) is False
            old_worker.commit()
            new_worker.refresh(second)
            assert second.status == JobStatus.RUNNING
            assert second.attempts == 2


def test_retryable_processing_error_keeps_batch_retryable(
    app_and_engine: tuple[object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        batch = ImportBatch(source_type="upload", file_count=1, status="queued")
        session.add(batch)
        session.flush()
        session.add(
            BackgroundJob(
                job_type="process_import_batch",
                resource_id=str(batch.id),
                max_attempts=2,
            )
        )
        session.commit()

        def fail_processing(*args: object, **kwargs: object) -> None:
            raise RuntimeError("temporary failure")

        monkeypatch.setattr("app.imports.tasks.process_import_batch", fail_processing)
        result = process_next_job(session, app.state.settings)

        assert result is not None
        session.expire_all()
        job = session.scalar(
            __import__("sqlalchemy")
            .select(BackgroundJob)
            .where(BackgroundJob.resource_id == str(batch.id))
        )
        refreshed_batch = session.get(ImportBatch, batch.id)
        assert job is not None
        assert refreshed_batch is not None
        assert job.status == JobStatus.RETRY_DUE
        assert job.attempts == 1
        assert refreshed_batch.status == "queued"


def test_terminal_processing_error_marks_batch_failed(
    app_and_engine: tuple[object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        batch = ImportBatch(source_type="upload", file_count=1, status="queued")
        session.add(batch)
        session.flush()
        session.add(
            BackgroundJob(
                job_type="process_import_batch",
                resource_id=str(batch.id),
                max_attempts=1,
            )
        )
        session.commit()

        def fail_processing(*args: object, **kwargs: object) -> None:
            raise RuntimeError("permanent failure")

        monkeypatch.setattr("app.imports.tasks.process_import_batch", fail_processing)
        process_next_job(session, app.state.settings)

        session.expire_all()
        job = session.scalar(
            __import__("sqlalchemy")
            .select(BackgroundJob)
            .where(BackgroundJob.resource_id == str(batch.id))
        )
        refreshed_batch = session.get(ImportBatch, batch.id)
        assert job is not None
        assert refreshed_batch is not None
        assert job.status == JobStatus.FAILED
        assert refreshed_batch.status == "failed"
        assert refreshed_batch.ended_at is not None


def test_non_retryable_failure_is_terminal(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        job = BackgroundJob(job_type="process_import_batch", resource_id="1")
        session.add(job)
        session.commit()

        claimed = claim_next_job(session, now=now)
        fail_job(session, claimed, "invalid_format", retryable=False, now=now)

        assert claimed.status == JobStatus.FAILED
        assert claimed.error_code == "invalid_format"


def test_claim_rejects_unrelated_uncommitted_work(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        session.add(ImportBatch(source_type="upload"))
        with pytest.raises(RuntimeError, match="job_claim_requires_clean_session"):
            claim_next_job(session)


def test_lost_lease_rolls_back_worker_business_changes(
    app_and_engine: tuple[object, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        batch = ImportBatch(source_type="upload", file_count=1, status="queued")
        session.add(batch)
        session.flush()
        session.add(
            BackgroundJob(job_type="process_import_batch", resource_id=str(batch.id))
        )
        session.commit()

        def write_business_state(
            worker_session: Session, batch_id: int, settings: object
        ) -> None:
            worker_batch = worker_session.get(ImportBatch, batch_id)
            assert worker_batch is not None
            worker_batch.status = "completed"

        monkeypatch.setattr(
            "app.imports.tasks.process_import_batch", write_business_state
        )
        monkeypatch.setattr(
            "app.imports.tasks.finish_job", lambda *args, **kwargs: False
        )

        process_next_job(session, app.state.settings)
        session.expire_all()
        persisted = session.get(ImportBatch, batch.id)
        assert persisted is not None
        assert persisted.status == "queued"
