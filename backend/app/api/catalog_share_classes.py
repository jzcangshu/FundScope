"""Fund share-class maintenance routes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import Field, model_validator
from sqlalchemy import select

from app.api.catalog_shared import (
    CatalogOperator,
    DatabaseSession,
    StrictModel,
    _assert_share_code_available,
    _audit,
    _commit,
    _flush,
    _fund_or_404,
    _optional_text,
    _required_text,
    _share_class_data,
    _share_class_or_404,
    _validate_date_range,
    _validate_date_range_request,
)
from app.auth.fund_access import require_fund_access
from app.db.models import ShareClass

router = APIRouter(tags=["catalog"])


class ShareClassCreate(StrictModel):
    share_code: str = Field(min_length=1, max_length=100)
    share_name: str = Field(min_length=1, max_length=255)
    enabled_from: date | None = None
    disabled_from: date | None = None
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for field in ("share_code", "share_name"):
            if isinstance(result.get(field), str):
                result[field] = _required_text(result[field])
        if isinstance(result.get("notes"), str):
            result["notes"] = _optional_text(result["notes"])
        return result

    @model_validator(mode="after")
    def validate_dates(self) -> ShareClassCreate:
        _validate_date_range(self.enabled_from, self.disabled_from, "share class")
        return self


class ShareClassUpdate(StrictModel):
    share_code: str | None = Field(default=None, min_length=1, max_length=100)
    share_name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled_from: date | None = None
    disabled_from: date | None = None
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for field in ("share_code", "share_name"):
            if isinstance(result.get(field), str):
                result[field] = _required_text(result[field])
        if isinstance(result.get("notes"), str):
            result["notes"] = _optional_text(result["notes"])
        return result


class ShareClassDisableRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)
    disabled_from: date | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_reason(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if isinstance(result.get("reason"), str):
            result["reason"] = _required_text(result["reason"])
        return result


@router.get("/funds/{fund_id}/share-classes")
def list_share_classes(
    fund_id: int,
    context: CatalogOperator,
    session: DatabaseSession,
    share_status: Literal["active", "inactive"] | None = Query(
        default=None, alias="status"
    ),
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    _fund_or_404(session, fund_id)
    classes = session.scalars(
        select(ShareClass)
        .where(ShareClass.fund_id == fund_id)
        .order_by(ShareClass.share_code, ShareClass.id)
    ).all()
    if share_status is not None:
        classes = [
            item
            for item in classes
            if ("inactive" if item.disabled_from else "active") == share_status
        ]
    return {
        "data": [_share_class_data(item) for item in classes],
        "meta": {"total": len(classes)},
    }


@router.post("/funds/{fund_id}/share-classes", status_code=status.HTTP_201_CREATED)
def create_share_class(
    fund_id: int,
    payload: ShareClassCreate,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    _fund_or_404(session, fund_id)
    _assert_share_code_available(session, fund_id, payload.share_code)
    share_class = ShareClass(
        fund_id=fund_id,
        share_code=payload.share_code,
        share_name=payload.share_name,
        enabled_from=payload.enabled_from,
        disabled_from=payload.disabled_from,
        notes=payload.notes,
    )
    session.add(share_class)
    _flush(session, "Share code already exists")
    _audit(
        session,
        context,
        action="share_class.create",
        resource_type="share_class",
        resource_id=share_class.id,
    )
    _commit(session, "Share code already exists")
    return {"data": _share_class_data(share_class)}


@router.patch("/funds/{fund_id}/share-classes/{share_class_id}")
def update_share_class(
    fund_id: int,
    share_class_id: int,
    payload: ShareClassUpdate,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    share_class = _share_class_or_404(session, fund_id, share_class_id)
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")
    next_from = values.get("enabled_from", share_class.enabled_from)
    next_to = values.get("disabled_from", share_class.disabled_from)
    _validate_date_range_request(next_from, next_to, "share class")
    if "share_code" in values:
        _assert_share_code_available(
            session,
            fund_id,
            values["share_code"],
            exclude_share_class_id=share_class.id,
        )
    for field, value in values.items():
        setattr(share_class, field, value)
    _audit(
        session,
        context,
        action="share_class.update",
        resource_type="share_class",
        resource_id=share_class.id,
        summary={"fields": sorted(values)},
    )
    _commit(session, "Share code already exists")
    return {"data": _share_class_data(share_class)}


@router.post("/funds/{fund_id}/share-classes/{share_class_id}/disable")
def disable_share_class(
    fund_id: int,
    share_class_id: int,
    payload: ShareClassDisableRequest,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    share_class = _share_class_or_404(session, fund_id, share_class_id)
    share_class.disabled_from = payload.disabled_from or datetime.now(UTC).date()
    _validate_date_range_request(
        share_class.enabled_from, share_class.disabled_from, "share class"
    )
    _audit(
        session,
        context,
        action="share_class.disable",
        resource_type="share_class",
        resource_id=share_class.id,
        reason=payload.reason,
    )
    _commit(session)
    return {"data": _share_class_data(share_class)}


@router.post("/funds/{fund_id}/share-classes/{share_class_id}/enable")
def enable_share_class(
    fund_id: int,
    share_class_id: int,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    share_class = _share_class_or_404(session, fund_id, share_class_id)
    share_class.disabled_from = None
    _audit(
        session,
        context,
        action="share_class.enable",
        resource_type="share_class",
        resource_id=share_class.id,
    )
    _commit(session)
    return {"data": _share_class_data(share_class)}
