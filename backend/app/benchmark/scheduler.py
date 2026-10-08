"""Background benchmark sync scheduler.

Runs inside the FastAPI lifespan, checking every 60 seconds whether a daily
benchmark sync is due.  The sync itself is dispatched as a BackgroundJob so
it runs in the worker process (not the API process).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import AuditResult, JobStatus
from app.db.models import AuditLog, BackgroundJob

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60

# Trigger at 18:00 Beijing time = 10:00 UTC.
_TRIGGER_HOUR_UTC = 10
_TRIGGER_MINUTE_UTC = 0


class BenchmarkSyncScheduler:
    """Check daily and enqueue a benchmark_sync job when due."""

    def __init__(self, engine: Engine, app_settings: Settings) -> None:
        self.engine = engine
        self.app_settings = app_settings
        self._triggered_today: str = ""

    async def run(self) -> None:
        logger.info(
            "benchmark sync scheduler started (poll every %ds)",
            POLL_INTERVAL_SECONDS,
        )
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                self._tick()
            except asyncio.CancelledError:
                logger.info("benchmark sync scheduler stopped")
                raise
            except Exception:
                logger.exception("benchmark sync scheduler tick failed")

    def _tick(self) -> None:
        if not self.app_settings.tushare_token:
            return  # No token configured; nothing to do.

        now_utc = datetime.now(UTC)
        today_key = now_utc.strftime("%Y-%m-%d")

        # Only trigger once per day.
        if self._triggered_today == today_key:
            return

        # Check if it's the right time (within the poll window).
        if now_utc.hour != _TRIGGER_HOUR_UTC or now_utc.minute != _TRIGGER_MINUTE_UTC:
            return

        with Session(self.engine) as session:
            if self._has_pending_job(session):
                self._triggered_today = today_key
                return

            self._enqueue(session, now_utc)
            session.commit()
            self._triggered_today = today_key
            logger.info("benchmark sync job enqueued by scheduler")

    def _has_pending_job(self, session: Session) -> bool:
        return (
            session.scalar(
                select(BackgroundJob.id)
                .where(
                    BackgroundJob.job_type == "benchmark_sync",
                    BackgroundJob.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
                )
                .limit(1)
            )
            is not None
        )

    def _enqueue(self, session: Session, now: datetime) -> None:
        run_id = uuid4().hex
        job = BackgroundJob(
            job_type="benchmark_sync",
            resource_id=run_id,
            status=JobStatus.PENDING,
            attempts=0,
            max_attempts=3,
            started_at=now,
            locked_at=None,
        )
        session.add(job)
        session.add(
            AuditLog(
                action="benchmark.sync_scheduled",
                resource_type="benchmark_sync",
                resource_id=run_id,
                summary={"trigger": "scheduler"},
                result=AuditResult.SUCCESS,
            )
        )
        session.flush()


__all__ = ["BenchmarkSyncScheduler"]
