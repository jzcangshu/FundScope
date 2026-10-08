"""Queue and serialize company calculations when the active fund set changes."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import AnalysisRunStatus, AuditResult, ValuationStatus
from app.db.models import (
    AnalysisRun,
    AuditLog,
    BackgroundJob,
    SystemState,
    ValuationVersion,
)

COMPANY_SCOPE_TRIGGER = "fund_scope_changed"
RISK_RULE_RECALC_TRIGGER = "risk_rule_recalculate"


def lock_company_scope(session: Session) -> None:
    """Keep a calculation's fund set stable through its transaction in production."""

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.scalar(
            select(SystemState.id).where(SystemState.id == 1).with_for_update()
        )


def queue_company_analysis_run(
    session: Session, *, fund_id: int, actor_user_id: int
) -> AnalysisRun:
    """Called with the scope lock, in the same transaction as the status change."""

    start_date = session.scalar(
        select(func.min(ValuationVersion.valuation_date)).where(
            ValuationVersion.status == ValuationStatus.PUBLISHED
        )
    )
    run = AnalysisRun(
        trigger_reason=COMPANY_SCOPE_TRIGGER,
        input_start_date=start_date or datetime.now(UTC).date(),
        input_version_range="company:active_funds",
        methodology_version="v1",
        status=AnalysisRunStatus.QUEUED,
    )
    session.add(run)
    session.flush()
    session.add_all(
        [
            BackgroundJob(job_type="process_analysis_run", resource_id=str(run.id)),
            AuditLog(
                actor_user_id=actor_user_id,
                action="analysis.queued",
                resource_type="analysis_run",
                resource_id=str(run.id),
                summary={"trigger_reason": COMPANY_SCOPE_TRIGGER, "fund_id": fund_id},
                result=AuditResult.SUCCESS,
            ),
        ]
    )
    session.flush()
    return run
