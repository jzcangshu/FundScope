"""Fund alias maintenance routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, status
from pydantic import Field, model_validator
from sqlalchemy import select

from app.api.catalog_shared import (
    CatalogOperator,
    DatabaseSession,
    StrictModel,
    _alias_data,
    _alias_or_404,
    _assert_alias_available,
    _audit,
    _commit,
    _flush,
    _fund_or_404,
    _optional_text,
    _required_text,
    _validate_date_range,
    _validate_date_range_request,
)
from app.auth.fund_access import require_fund_access
from app.db.models import FundAlias

router = APIRouter(tags=["catalog"])


class AliasInput(StrictModel):
    alias: str = Field(min_length=1, max_length=255)
    source_location: str | None = Field(default=None, max_length=255)
    match_priority: int = Field(default=0, ge=0, le=1000)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if isinstance(result.get("alias"), str):
            result["alias"] = _required_text(result["alias"])
        if isinstance(result.get("source_location"), str):
            result["source_location"] = _optional_text(result["source_location"])
        return result

    @model_validator(mode="after")
    def validate_dates(self) -> AliasInput:
        _validate_date_range(self.valid_from, self.valid_to, "alias")
        return self


class AliasUpdate(StrictModel):
    alias: str | None = Field(default=None, min_length=1, max_length=255)
    source_location: str | None = Field(default=None, max_length=255)
    match_priority: int | None = Field(default=None, ge=0, le=1000)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if isinstance(result.get("alias"), str):
            result["alias"] = _required_text(result["alias"])
        if isinstance(result.get("source_location"), str):
            result["source_location"] = _optional_text(result["source_location"])
        return result

    @model_validator(mode="after")
    def validate_dates(self) -> AliasUpdate:
        _validate_date_range(self.valid_from, self.valid_to, "alias")
        return self


@router.get("/funds/{fund_id}/aliases")
def list_aliases(
    fund_id: int, context: CatalogOperator, session: DatabaseSession
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    _fund_or_404(session, fund_id)
    aliases = session.scalars(
        select(FundAlias)
        .where(FundAlias.fund_id == fund_id)
        .order_by(FundAlias.match_priority.desc(), FundAlias.id)
    ).all()
    return {
        "data": [_alias_data(item) for item in aliases],
        "meta": {"total": len(aliases)},
    }


@router.post("/funds/{fund_id}/aliases", status_code=status.HTTP_201_CREATED)
def create_alias(
    fund_id: int,
    payload: AliasInput,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    _fund_or_404(session, fund_id)
    _assert_alias_available(session, payload.alias, fund_id)
    alias = FundAlias(
        fund_id=fund_id,
        alias=payload.alias,
        source_location=payload.source_location,
        match_priority=payload.match_priority,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
    )
    session.add(alias)
    _flush(session, "Alias already exists")
    _audit(
        session,
        context,
        action="fund_alias.create",
        resource_type="fund_alias",
        resource_id=alias.id,
    )
    _commit(session, "Alias already exists")
    return {"data": _alias_data(alias)}


@router.patch("/funds/{fund_id}/aliases/{alias_id}")
def update_alias(
    fund_id: int,
    alias_id: int,
    payload: AliasUpdate,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    alias = _alias_or_404(session, fund_id, alias_id)
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "alias" in values:
        _assert_alias_available(
            session, values["alias"], fund_id, exclude_alias_id=alias.id
        )
    next_from = values.get("valid_from", alias.valid_from)
    next_to = values.get("valid_to", alias.valid_to)
    _validate_date_range_request(next_from, next_to, "alias")
    for field, value in values.items():
        setattr(alias, field, value)
    _audit(
        session,
        context,
        action="fund_alias.update",
        resource_type="fund_alias",
        resource_id=alias.id,
        summary={"fields": sorted(values)},
    )
    _commit(session, "Alias already exists")
    return {"data": _alias_data(alias)}


class DeleteAliasRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2000)


@router.delete("/funds/{fund_id}/aliases/{alias_id}")
def delete_alias(
    fund_id: int,
    alias_id: int,
    context: CatalogOperator,
    session: DatabaseSession,
    payload: DeleteAliasRequest | None = None,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    alias = _alias_or_404(session, fund_id, alias_id)
    resource_id = alias.id
    session.delete(alias)
    _audit(
        session,
        context,
        action="fund_alias.delete",
        resource_type="fund_alias",
        resource_id=resource_id,
        reason=payload.reason if payload else None,
    )
    _commit(session)
    return {"data": {"id": resource_id, "deleted": True}}
