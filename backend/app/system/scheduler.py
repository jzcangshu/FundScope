"""Background mail sync scheduler driven by database-backed schedule config."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import AuditResult, JobStatus
from app.db.models import AuditLog, BackgroundJob, SystemState
from app.system.settings import (
    effective_mail_sync_schedule,
    mail_sync_enabled,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


class MailSyncScheduler:
    """Check the mail sync schedule periodically and enqueue jobs when due."""

    def __init__(self, engine: Engine, app_settings: Settings) -> None:
        self.engine = engine
        self.app_settings = app_settings
        self._scheduled_triggered_today: set[str] = set()
        self._last_triggered_date: str = ""

    async def run(self) -> None:
        """Main loop — runs until the asyncio task is cancelled."""

        logger.info(
            "mail sync scheduler started (poll every %ds)", POLL_INTERVAL_SECONDS
        )
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                self._tick()
            except asyncio.CancelledError:
                logger.info("mail sync scheduler stopped")
                raise
            except Exception:
                logger.exception("mail sync scheduler tick failed")

    def _tick(self) -> None:
        now_utc = datetime.now(UTC)
        with Session(self.engine) as session:
            if not mail_sync_enabled(session):
                return
            if self._has_running_sync(session):
                return
            schedule = effective_mail_sync_schedule(session)
            if self._is_due(schedule, now_utc, session):
                self._trigger_sync(session, now_utc)
                session.commit()

    def _has_running_sync(self, session: Session) -> bool:
        return (
            session.scalar(
                select(BackgroundJob.id)
                .where(
                    BackgroundJob.job_type == "mail_sync",
                    BackgroundJob.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
                )
                .limit(1)
            )
            is not None
        )

    def _is_due(
        self, schedule: dict[str, object], now_utc: datetime, session: Session
    ) -> bool:
        mode = schedule.get("mode", "interval")
        if mode == "interval":
            return self._is_due_interval(schedule, now_utc, session)
        return self._is_due_scheduled(schedule, now_utc, session)

    def _is_due_interval(
        self, schedule: dict[str, object], now_utc: datetime, session: Session
    ) -> bool:
        interval = schedule.get("interval_minutes", 30)
        if not isinstance(interval, int) or interval < 1:
            interval = 30
        # Always read the last sync completion from the database so the
        # interval measures from when the previous sync finished, not when
        # it was enqueued. This prevents overlapping syncs when a sync takes
        # longer than the poll interval.
        last = self._get_last_sync_time(session)
        if last is None:
            return True
        elapsed = (now_utc - last).total_seconds()
        return elapsed >= interval * 60

    def _is_due_scheduled(
        self, schedule: dict[str, object], now_utc: datetime, _session: Session
    ) -> bool:
        times = schedule.get("times")
        if not isinstance(times, list) or not times:
            return False
        tz_name = self._get_timezone()
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("Asia/Shanghai")
        local_now = now_utc.astimezone(tz)
        today_key = local_now.strftime("%Y-%m-%d")
        if today_key != self._last_triggered_date:
            self._scheduled_triggered_today.clear()
            self._last_triggered_date = today_key
        current_hm = f"{local_now.hour:02d}:{local_now.minute:02d}"
        weekday = local_now.isoweekday()  # 1=Mon, 7=Sun
        for entry in times:
            if not isinstance(entry, dict):
                continue
            t = entry.get("time", "")
            if t != current_hm:
                continue
            days = entry.get("days", [])
            if isinstance(days, list) and days and weekday not in days:
                continue
            slot_key = f"{current_hm}:{','.join(str(d) for d in (days or []))}"
            if slot_key in self._scheduled_triggered_today:
                continue
            self._scheduled_triggered_today.add(slot_key)
            return True
        return False

    def _get_timezone(self) -> str:
        with Session(self.engine) as session:
            state = session.get(SystemState, 1)
            if state is not None and isinstance(state.settings, dict):
                tz = state.settings.get("timezone")
                if isinstance(tz, str) and tz.strip():
                    return tz.strip()
        return "Asia/Shanghai"

    def _get_last_sync_time(self, session: Session) -> datetime | None:
        audit = session.scalar(
            select(AuditLog.created_at)
            .where(
                AuditLog.action.in_(("mail.sync_completed", "mail.sync_failed")),
                AuditLog.resource_type == "mail_sync",
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
        return audit

    def _trigger_sync(self, session: Session, now: datetime) -> None:
        run_id = uuid4().hex
        job = BackgroundJob(
            job_type="mail_sync",
            resource_id=run_id,
            status=JobStatus.PENDING,
            attempts=0,
            max_attempts=1,
            started_at=now,
            locked_at=None,
        )
        session.add(job)
        session.add(
            AuditLog(
                action="mail.sync_scheduled",
                resource_type="mail_sync",
                resource_id=run_id,
                summary={"trigger": "scheduler"},
                result=AuditResult.SUCCESS,
            )
        )
        session.add(
            AuditLog(
                action="mail.sync_started",
                resource_type="mail_sync",
                resource_id=run_id,
                summary={"trigger": "scheduler"},
                result=AuditResult.SUCCESS,
            )
        )
        session.flush()
        logger.info("mail sync job enqueued by scheduler: %s", run_id)


__all__ = ["MailSyncScheduler"]
