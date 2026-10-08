"""Risk-rule versioning and risk-event operations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics.scope import RISK_RULE_RECALC_TRIGGER
from app.auth.dependencies import AuthContext, get_auth_context, get_db, require_roles
from app.auth.fund_access import (
    require_fund_access,
    require_unrestricted_access,
    visible_fund_ids,
)
from app.auth.service import AuthService
from app.db.base import (
    AnalysisRunStatus,
    RiskEventStatus,
    RiskSeverity,
    UserRole,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    Fund,
    RiskEvent,
    RiskRule,
    ValuationVersion,
)
from app.publishing import PublishingService

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])

DatabaseSession = Annotated[Session, Depends(get_db)]
RiskReader = Annotated[AuthContext, Depends(get_auth_context)]
RiskRuleOperator = Annotated[
    AuthContext,
    Depends(
        require_roles(UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER, UserRole.BOSS)
    ),
]
RiskEventOperator = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]

SUPPORTED_RULE_TYPES = {
    "daily_return",
    "max_drawdown",
    "current_drawdown",
    "single_position_weight",
    "top_five_weight",
    "concentration",
}


class RiskRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_code: str = Field(min_length=1, max_length=100)
    rule_type: str = Field(min_length=1, max_length=100)
    scope: str = Field(default="all", min_length=1, max_length=100)
    threshold: Decimal
    severity: RiskSeverity = RiskSeverity.WARNING
    valid_from: date | None = None
    valid_to: date | None = None
    version: str | None = Field(default=None, max_length=50)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_period(self) -> RiskRuleCreate:
        if (
            not self.rule_code.strip()
            or not self.rule_type.strip()
            or not self.scope.strip()
        ):
            raise ValueError("rule fields must not be blank")
        if not self.threshold.is_finite():
            raise ValueError("threshold must be finite")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not be earlier than valid_from")
        return self


class RiskRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_type: str | None = Field(default=None, min_length=1, max_length=100)
    scope: str | None = Field(default=None, min_length=1, max_length=100)
    threshold: Decimal | None = None
    severity: RiskSeverity | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_period(self) -> RiskRulePatch:
        if self.rule_type is not None and not self.rule_type.strip():
            raise ValueError("rule_type must not be blank")
        if self.scope is not None and not self.scope.strip():
            raise ValueError("scope must not be blank")
        if "threshold" in self.model_fields_set and self.threshold is None:
            raise ValueError("threshold cannot be cleared")
        if self.threshold is not None and not self.threshold.is_finite():
            raise ValueError("threshold must be finite")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not be earlier than valid_from")
        return self


class RiskEventHandling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RiskEventStatus
    handling_note: str = Field(min_length=1, max_length=4000)
    evidence_reference: str | None = Field(default=None, max_length=1000)


def _value(value: object) -> object:
    return value.value if hasattr(value, "value") else value


def _rule_data(rule: RiskRule) -> dict[str, object]:
    return {
        "id": rule.id,
        "rule_code": rule.rule_code,
        "rule_type": rule.rule_type,
        "scope": rule.scope,
        "threshold": str(rule.threshold) if rule.threshold is not None else None,
        "severity": _value(rule.severity),
        "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
        "valid_to": rule.valid_to.isoformat() if rule.valid_to else None,
        "version": rule.version,
        "enabled": rule.enabled,
    }


def _event_data(
    event: RiskEvent,
    *,
    rule_code: str | None = None,
    fund_name: str | None = None,
) -> dict[str, object]:
    return {
        "id": event.id,
        "risk_rule_id": event.risk_rule_id,
        "rule_code": rule_code,
        "fund_id": event.fund_id,
        "fund_name": fund_name,
        "valuation_date": event.valuation_date.isoformat(),
        "severity": _value(event.severity),
        "status": _value(event.status),
        "first_triggered_at": event.first_triggered_at.isoformat(),
        "last_triggered_at": event.last_triggered_at.isoformat(),
        "handling_note": event.handling_note,
        "evidence_snapshot": event.evidence_snapshot,
        "handled_by_user_id": event.handled_by_user_id,
        "handled_at": event.handled_at.isoformat() if event.handled_at else None,
        "evidence_reference": event.evidence_reference,
        "created_at": event.created_at.isoformat(),
    }


def _validate_rule_type(rule_type: str) -> str:
    normalized = rule_type.strip()
    if normalized not in SUPPORTED_RULE_TYPES:
        raise HTTPException(status_code=422, detail="Unsupported risk rule type")
    return normalized


def _next_version(session: Session, rule_code: str) -> str:
    versions = session.scalars(
        select(RiskRule.version).where(RiskRule.rule_code == rule_code)
    ).all()
    numeric = [int(version) for version in versions if str(version).isdigit()]
    return str(max(numeric, default=0) + 1)


@router.get("/rules")
def list_rules(
    context: RiskReader,
    session: DatabaseSession,
    rule_code: str | None = Query(default=None, max_length=100),
    enabled: bool | None = None,
    include_history: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    statement = select(RiskRule).order_by(RiskRule.rule_code, RiskRule.id.desc())
    if rule_code:
        statement = statement.where(RiskRule.rule_code == rule_code.strip())
    if enabled is not None:
        statement = statement.where(RiskRule.enabled == enabled)
    if not include_history:
        latest_id = (
            select(func.max(RiskRule.id)).group_by(RiskRule.rule_code).scalar_subquery()
        )
        statement = statement.where(RiskRule.id.in_(latest_id))
    count_statement = select(func.count(RiskRule.id))
    if rule_code:
        count_statement = count_statement.where(RiskRule.rule_code == rule_code.strip())
    if enabled is not None:
        count_statement = count_statement.where(RiskRule.enabled == enabled)
    if not include_history:
        count_statement = count_statement.where(RiskRule.id.in_(latest_id))
    offset = (page - 1) * page_size
    total = session.scalar(count_statement) or 0
    rules = list(session.scalars(statement.offset(offset).limit(page_size)))
    return {
        "data": [_rule_data(rule) for rule in rules],
        "meta": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "include_history": include_history,
        },
    }


@router.post("/rules", status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: RiskRuleCreate,
    context: RiskRuleOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    rule_code = payload.rule_code.strip()
    rule_type = _validate_rule_type(payload.rule_type)
    version = payload.version or _next_version(session, rule_code)
    rule = RiskRule(
        rule_code=rule_code,
        rule_type=rule_type,
        scope=payload.scope.strip(),
        threshold=payload.threshold,
        severity=payload.severity,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        version=version,
        enabled=payload.enabled,
    )
    session.add(rule)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Risk rule version already exists"
        ) from None
    AuthService(session).record_audit(
        action="risk_rule.version_created",
        resource_type="risk_rule",
        resource_id=str(rule.id),
        actor_user_id=context.user.id,
        summary={"rule_code": rule.rule_code, "version": rule.version},
    )
    session.commit()
    return {"data": _rule_data(rule)}


@router.patch("/rules/{rule_id}")
def patch_rule(
    rule_id: int,
    payload: RiskRulePatch,
    context: RiskRuleOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    current = session.get(RiskRule, rule_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Risk rule not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=422, detail="At least one rule field is required"
        )
    rule_type = _validate_rule_type(str(changes.get("rule_type", current.rule_type)))
    valid_from = changes.get("valid_from", current.valid_from)
    valid_to = changes.get("valid_to", current.valid_to)
    if valid_from and valid_to and valid_to < valid_from:
        raise HTTPException(status_code=422, detail="Invalid rule validity period")
    new_rule = RiskRule(
        rule_code=current.rule_code,
        rule_type=rule_type,
        scope=str(changes.get("scope", current.scope)).strip(),
        threshold=changes.get("threshold", current.threshold),
        severity=changes.get("severity", current.severity),
        valid_from=valid_from,
        valid_to=valid_to,
        version=_next_version(session, current.rule_code),
        enabled=changes.get("enabled", current.enabled),
    )
    session.add(new_rule)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Risk rule version already exists"
        ) from None
    AuthService(session).record_audit(
        action="risk_rule.version_created",
        resource_type="risk_rule",
        resource_id=str(new_rule.id),
        actor_user_id=context.user.id,
        summary={
            "rule_code": new_rule.rule_code,
            "version": new_rule.version,
            "previous_rule_id": current.id,
            "changed_fields": sorted(changes),
        },
    )
    session.commit()
    return {"data": _rule_data(new_rule)}


@router.post("/rules/recalculate", status_code=status.HTTP_202_ACCEPTED)
def recalculate_rules(
    context: RiskEventOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    """Queue full-history analysis runs so the latest rule versions apply everywhere.

    One run per active fund, starting at its earliest published valuation;
    the worker recomputes metrics/events asynchronously and closes events
    the current rules no longer trigger.
    """
    require_unrestricted_access(session, context.user)

    in_flight = session.scalar(
        select(func.count(AnalysisRun.id)).where(
            AnalysisRun.trigger_reason == RISK_RULE_RECALC_TRIGGER,
            AnalysisRun.status.in_(
                (AnalysisRunStatus.QUEUED, AnalysisRunStatus.RUNNING)
            ),
        )
    )
    if in_flight:
        raise HTTPException(
            status_code=409, detail="规则重算正在进行中，请稍后查看结果"
        )
    date_window = (
        select(
            ValuationVersion.fund_id.label("fund_id"),
            func.min(ValuationVersion.valuation_date).label("earliest_date"),
            func.max(ValuationVersion.valuation_date).label("latest_date"),
        )
        .join(Fund, Fund.id == ValuationVersion.fund_id)
        .where(
            ValuationVersion.status == ValuationStatus.PUBLISHED,
            Fund.status == "active",
        )
        .group_by(ValuationVersion.fund_id)
        .subquery()
    )
    rows = session.execute(
        select(
            ValuationVersion.id,
            ValuationVersion.fund_id,
            date_window.c.earliest_date,
        )
        .join(
            date_window,
            (ValuationVersion.fund_id == date_window.c.fund_id)
            & (ValuationVersion.valuation_date == date_window.c.latest_date),
        )
        .where(ValuationVersion.status == ValuationStatus.PUBLISHED)
        .order_by(ValuationVersion.fund_id, ValuationVersion.id.desc())
    ).all()
    if not rows:
        raise HTTPException(
            status_code=422, detail="没有已发布估值的启用产品，无需重算"
        )
    publisher = PublishingService(session)
    queued: list[dict[str, object]] = []
    seen_funds: set[int] = set()
    for version_id, fund_id, earliest_date in rows:
        if fund_id in seen_funds:
            continue
        seen_funds.add(fund_id)
        run = publisher.queue_analysis_run(
            version_id,
            start_date=earliest_date,
            actor_user_id=context.user.id,
            trigger_reason=RISK_RULE_RECALC_TRIGGER,
        )
        queued.append(
            {
                "analysis_run_id": run.id,
                "fund_id": fund_id,
                "start_date": earliest_date.isoformat(),
            }
        )
    AuthService(session).record_audit(
        action="risk_rule.recalculate_queued",
        resource_type="risk_rule",
        resource_id=None,
        actor_user_id=context.user.id,
        summary={"queued_count": len(queued), "fund_ids": sorted(seen_funds)},
    )
    session.commit()
    return {"data": {"queued_count": len(queued), "runs": queued}}


@router.get("/events")
def list_events(
    context: RiskReader,
    session: DatabaseSession,
    fund_id: int | None = None,
    rule_code: str | None = Query(default=None, max_length=100),
    severity: RiskSeverity | None = None,
    status_filter: RiskEventStatus | None = Query(default=None, alias="status"),  # noqa: B008
    start: date | None = None,
    end: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    if start and end and end < start:
        raise HTTPException(
            status_code=422, detail="end must not be earlier than start"
        )
    allowed_ids = visible_fund_ids(session, context.user)
    filters = []
    if allowed_ids is not None:
        filters.append(RiskEvent.fund_id.in_(allowed_ids))
    if fund_id is not None:
        require_fund_access(session, context.user, fund_id)
        filters.append(RiskEvent.fund_id == fund_id)
    if rule_code:
        filters.append(RiskRule.rule_code == rule_code.strip())
    if severity is not None:
        filters.append(RiskEvent.severity == severity)
    if status_filter is not None:
        filters.append(RiskEvent.status == status_filter)
    if start is not None:
        filters.append(RiskEvent.valuation_date >= start)
    if end is not None:
        filters.append(RiskEvent.valuation_date <= end)
    base = select(RiskEvent).join(RiskRule, RiskRule.id == RiskEvent.risk_rule_id)
    count_statement = (
        select(func.count(RiskEvent.id))
        .select_from(RiskEvent)
        .join(RiskRule, RiskRule.id == RiskEvent.risk_rule_id)
    )
    if filters:
        base = base.where(*filters)
        count_statement = count_statement.where(*filters)
    total = session.scalar(count_statement) or 0
    events = list(
        session.scalars(
            base.order_by(RiskEvent.valuation_date.desc(), RiskEvent.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    rule_ids = {event.risk_rule_id for event in events}
    rules = (
        {
            rule.id: rule.rule_code
            for rule in session.scalars(
                select(RiskRule).where(RiskRule.id.in_(rule_ids))
            )
        }
        if rule_ids
        else {}
    )
    fund_ids = {event.fund_id for event in events if event.fund_id is not None}
    funds = (
        {
            fund.id: fund.standard_name
            for fund in session.scalars(select(Fund).where(Fund.id.in_(fund_ids)))
        }
        if fund_ids
        else {}
    )
    return {
        "data": [
            _event_data(
                event,
                rule_code=rules.get(event.risk_rule_id),
                fund_name=funds.get(event.fund_id),
            )
            for event in events
        ],
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


def _handle_event(
    event_id: int,
    payload: RiskEventHandling,
    context: AuthContext,
    session: Session,
) -> dict[str, object]:
    note = payload.handling_note.strip()
    if not note:
        raise HTTPException(status_code=422, detail="handling_note is required")
    if payload.status == RiskEventStatus.OPEN:
        raise HTTPException(status_code=422, detail="Event cannot be handled as open")
    event = session.get(RiskEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Risk event not found")
    allowed_ids = visible_fund_ids(session, context.user)
    if allowed_ids is not None and event.fund_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Risk event not found")
    event.status = payload.status
    event.handling_note = note
    evidence_reference = (
        payload.evidence_reference.strip() if payload.evidence_reference else None
    )
    event.evidence_reference = evidence_reference or None
    event.handled_by_user_id = context.user.id
    event.handled_at = datetime.now(UTC)
    AuthService(session).record_audit(
        action=f"risk_event.{payload.status.value}",
        resource_type="risk_event",
        resource_id=str(event.id),
        actor_user_id=context.user.id,
        summary={
            "status": payload.status.value,
            "evidence_reference_present": bool(event.evidence_reference),
        },
        reason=note,
    )
    session.commit()
    return {"data": _event_data(event)}


@router.post("/events/{event_id}/resolve")
def resolve_event(
    event_id: int,
    payload: RiskEventHandling,
    context: RiskEventOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    return _handle_event(event_id, payload, context, session)


@router.post("/events/{event_id}/handle")
def handle_event(
    event_id: int,
    payload: RiskEventHandling,
    context: RiskEventOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    return _handle_event(event_id, payload, context, session)


__all__ = ["router"]
