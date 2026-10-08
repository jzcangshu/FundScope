"""Administrator-only system settings and read-only audit queries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_db, require_roles
from app.auth.fund_access import require_unrestricted_access
from app.auth.service import AuthService
from app.db.base import AuditResult, UserRole
from app.db.models import AuditLog
from app.system.health import operational_summary
from app.system.maintenance import MaintenanceService
from app.system.settings import (
    RUNTIME_NOTE,
    SystemSettingsError,
    effective_settings,
    update_settings,
)

router = APIRouter(prefix="/api/v1", tags=["system"])

DatabaseSession = Annotated[Session, Depends(get_db)]
SystemAdmin = Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN))]
AuditReader = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]
SystemOperator = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]


class SystemSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    settings: dict[str, object] | None = None


class RetentionExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: Literal["DELETE_EXPIRED_SOURCE_FILES"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


def _setting_values(payload: SystemSettingsPatch) -> dict[str, object]:
    values = dict(payload.model_extra or {})
    if payload.settings is not None:
        values = {**payload.settings, **values}
    return values


@router.get("/system/settings")
def get_system_settings(
    _: SystemAdmin,
    request: Request,
    session: DatabaseSession,
) -> dict[str, object]:
    return {
        "data": effective_settings(session, request.app.state.settings),
        "meta": {"runtime_note": RUNTIME_NOTE},
    }


@router.patch("/system/settings")
def patch_system_settings(
    payload: SystemSettingsPatch,
    context: SystemAdmin,
    request: Request,
    session: DatabaseSession,
) -> dict[str, object]:
    values = _setting_values(payload)
    if not values:
        raise HTTPException(status_code=422, detail="At least one setting is required")
    try:
        settings = update_settings(session, request.app.state.settings, values)
    except SystemSettingsError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    AuthService(session).record_audit(
        action="system.settings_updated",
        resource_type="system_settings",
        resource_id="1",
        actor_user_id=context.user.id,
        summary={"changed_keys": sorted(values)},
    )
    session.commit()
    return {"data": settings, "meta": {"runtime_note": RUNTIME_NOTE}}


@router.get("/system/health")
def system_health(
    _: SystemAdmin,
    request: Request,
    session: DatabaseSession,
) -> dict[str, object]:
    """Return an authenticated, non-sensitive dependency health snapshot."""

    try:
        session.execute(select(1))
    except SQLAlchemyError:
        return {
            "data": {
                "status": "degraded",
                "database": "unavailable",
                "service": request.app.state.settings.service_name,
            }
        }
    return {
        "data": {
            "status": "ok",
            "database": "ok",
            "service": request.app.state.settings.service_name,
        }
    }


@router.get("/system/operations")
def system_operations(
    context: SystemOperator,
    request: Request,
    session: DatabaseSession,
) -> dict[str, object]:
    """Return the authenticated maintenance and worker operations summary."""
    require_unrestricted_access(session, context.user)

    return {"data": operational_summary(session, request.app.state.settings)}


@router.post("/system/retention/preview")
def preview_source_retention(
    context: SystemAdmin,
    request: Request,
    session: DatabaseSession,
) -> dict[str, object]:
    result = MaintenanceService(
        session,
        request.app.state.settings,
        actor_user_id=context.user.id,
    ).run("source-retention", dry_run=True)
    return {"data": result.as_dict()}


@router.post("/system/retention/execute")
def execute_source_retention(
    payload: RetentionExecutionRequest,
    context: SystemAdmin,
    request: Request,
    session: DatabaseSession,
) -> dict[str, object]:
    result = MaintenanceService(
        session,
        request.app.state.settings,
        actor_user_id=context.user.id,
    ).run("source-retention", dry_run=False, reason=payload.reason)
    return {"data": result.as_dict()}


_SENSITIVE_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "credential",
    "authorization",
    "database_url",
    "dsn",
    "connection",
    "command",
)

_SENSITIVE_TEXT_MARKERS = (
    "password",
    "token",
    "secret",
    "credential",
    "authorization",
    "postgresql://",
    "mysql://",
    "sqlite:///",
)


def _safe_summary(value: object, key: str | None = None) -> object:
    normalized_key = key.casefold() if key else ""
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(child_key): _safe_summary(child_value, str(child_key))
            for child_key, child_value in value.items()
            if not any(
                part in str(child_key).casefold() for part in _SENSITIVE_KEY_PARTS
            )
        }
    if isinstance(value, list):
        return [_safe_summary(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_summary(item) for item in value]
    if isinstance(value, str) and any(
        marker in value.casefold() for marker in _SENSITIVE_TEXT_MARKERS
    ):
        return "[redacted]"
    return value


@router.get("/audit-logs")
def list_audit_logs(
    context: AuditReader,
    session: DatabaseSession,
    actor_user_id: int | None = None,
    action: str | None = Query(default=None, max_length=100),
    resource_type: str | None = Query(default=None, max_length=100),
    result: AuditResult | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    if any(
        value is not None and (value.tzinfo is None or value.utcoffset() is None)
        for value in (start, end)
    ):
        raise HTTPException(
            status_code=422, detail="start and end must include a timezone"
        )
    if start and end and end < start:
        raise HTTPException(
            status_code=422, detail="end must not be earlier than start"
        )
    filters = []
    if actor_user_id is not None:
        filters.append(AuditLog.actor_user_id == actor_user_id)
    if action:
        filters.append(AuditLog.action == action.strip())
    if resource_type:
        filters.append(AuditLog.resource_type == resource_type.strip())
    if result is not None:
        filters.append(AuditLog.result == result)
    if start is not None:
        filters.append(AuditLog.created_at >= start)
    if end is not None:
        filters.append(AuditLog.created_at <= end)
    statement = select(AuditLog).order_by(
        AuditLog.created_at.desc(), AuditLog.id.desc()
    )
    count_statement = select(func.count(AuditLog.id))
    if filters:
        statement = statement.where(*filters)
        count_statement = count_statement.where(*filters)
    total = session.scalar(count_statement) or 0
    rows = list(
        session.scalars(statement.offset((page - 1) * page_size).limit(page_size))
    )
    data = [
        {
            "id": row.id,
            "actor_user_id": row.actor_user_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "summary": _safe_summary(row.summary),
            "reason": _safe_summary(row.reason),
            "result": row.result.value if hasattr(row.result, "value") else row.result,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
    return {
        "data": data,
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


__all__ = ["router"]
