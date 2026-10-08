"""Small standalone worker process for database-backed import jobs."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db.session import create_engine
from .imports.tasks import process_next_job
from .system.health import record_worker_heartbeat

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return os.getenv("WORKER_ID", "worker-default")


def _record_heartbeat_if_supported(session: Session, worker_id: str) -> None:
    """Keep heartbeat persistence best effort so it cannot stop job processing."""

    try:
        record_worker_heartbeat(session, worker_id=worker_id)
        session.commit()
    except Exception:  # noqa: BLE001 - health telemetry must not stop the worker
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            rollback()


def run_worker(
    settings: Settings | None = None,
    *,
    max_jobs: int | None = None,
    idle_sleep_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Process jobs serially until the optional limit is reached.

    A new session is used for every job so a parser failure or a lost lease
    cannot contaminate the next claim.
    """

    runtime = settings or get_settings()
    engine = create_engine(runtime.database_url)
    completed = 0
    worker_id = _worker_id()
    logger.info("worker started (id=%s)", worker_id)
    try:
        while max_jobs is None or completed < max_jobs:
            with Session(engine) as session:
                result = process_next_job(session, runtime)
                _record_heartbeat_if_supported(session, worker_id)
                if result is not None:
                    job, _ = result
                    try:
                        logger.info(
                            "job %s type=%s status=%s",
                            job.id,
                            job.job_type,
                            job.status.value
                            if hasattr(job.status, "value")
                            else job.status,
                        )
                    except Exception:  # noqa: BLE001 - logging must not crash worker
                        logger.info("job processed (details unavailable)")
            if result is None:
                if max_jobs is not None:
                    break
                sleep(idle_sleep_seconds)
                continue
            completed += 1
    except Exception:
        logger.exception("worker loop crashed")
        raise
    finally:
        engine.dispose()
        logger.info("worker stopped after %d jobs", completed)
    return completed


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fund dashboard import worker")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--idle-sleep", type=float, default=5.0)
    args = parser.parse_args()
    if args.max_jobs is not None and args.max_jobs < 1:
        parser.error("--max-jobs must be positive")
    if args.idle_sleep < 0:
        parser.error("--idle-sleep must be non-negative")
    run_worker(max_jobs=args.max_jobs, idle_sleep_seconds=args.idle_sleep)


if __name__ == "__main__":
    main()
