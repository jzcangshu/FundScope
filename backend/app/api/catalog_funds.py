"""Fund master-data maintenance routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, status
from pydantic import Field, model_validator
from sqlalchemy import select

from app.analytics.scope import lock_company_scope, queue_company_analysis_run
from app.api.catalog_aliases import AliasInput
from app.api.catalog_shared import (
    CatalogAdmin,
    CatalogOperator,
    DatabaseSession,
    StrictModel,
    _assert_alias_available,
    _assert_fund_name_available,
    _assert_product_code_available,
    _audit,
    _commit,
    _flush,
    _fund_data,
    _fund_or_404,
    _optional_text,
    _required_text,
)
from app.auth.fund_access import require_fund_access, require_unrestricted_access
from app.catalog.fund_removal import collect_fund_removal_impact, remove_fund
from app.db.base import FundStatus
from app.db.models import Fund, FundAlias

router = APIRouter(tags=["catalog"])


class FundCreate(StrictModel):
    standard_name: str = Field(min_length=1, max_length=255)
    product_code: str = Field(min_length=1, max_length=100)
    establishment_date: date
    strategy: str | None = Field(default=None, max_length=255)
    manager: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    aliases: list[AliasInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for field in ("standard_name", "product_code", "strategy", "manager", "notes"):
            if isinstance(result.get(field), str):
                result[field] = (
                    _required_text(result[field])
                    if field == "standard_name"
                    else _optional_text(result[field])
                )
        return result


class FundUpdate(StrictModel):
    standard_name: str | None = Field(default=None, min_length=1, max_length=255)
    product_code: str | None = Field(default=None, max_length=100)
    establishment_date: date | None = None
    strategy: str | None = Field(default=None, max_length=255)
    manager: str | None = Field(default=None, max_length=255)
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for field in ("standard_name", "product_code", "strategy", "manager", "notes"):
            if isinstance(result.get(field), str):
                result[field] = (
                    _required_text(result[field])
                    if field == "standard_name"
                    else _optional_text(result[field])
                )
        return result


class ReasonRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def trim_reason(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if isinstance(result.get("reason"), str):
            result["reason"] = _required_text(result["reason"])
        return result


@router.post("/funds", status_code=status.HTTP_201_CREATED)
def create_fund(
    payload: FundCreate, context: CatalogOperator, session: DatabaseSession
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    _assert_fund_name_available(session, payload.standard_name)
    _assert_product_code_available(session, payload.product_code)
    aliases_seen: set[str] = set()
    for item in payload.aliases:
        normalized_alias = item.alias.strip().casefold()
        if normalized_alias in aliases_seen:
            raise HTTPException(status_code=409, detail="Alias already exists")
        aliases_seen.add(normalized_alias)
        if normalized_alias == payload.standard_name.strip().casefold():
            raise HTTPException(
                status_code=409, detail="Alias must differ from fund name"
            )
        _assert_alias_available(session, item.alias, 0)

    fund = Fund(
        standard_name=payload.standard_name,
        product_code=payload.product_code,
        establishment_date=payload.establishment_date,
        strategy=payload.strategy,
        manager=payload.manager,
        notes=payload.notes,
        status=FundStatus.ACTIVE,
    )
    session.add(fund)
    _flush(session, "Fund name or product code already exists")
    for item in payload.aliases:
        session.add(
            FundAlias(
                fund_id=fund.id,
                alias=item.alias,
                source_location=item.source_location,
                match_priority=item.match_priority,
                valid_from=item.valid_from,
                valid_to=item.valid_to,
            )
        )
    _flush(session, "Alias already exists")
    for alias in session.scalars(
        select(FundAlias).where(FundAlias.fund_id == fund.id)
    ).all():
        _audit(
            session,
            context,
            action="fund_alias.create",
            resource_type="fund_alias",
            resource_id=alias.id,
        )
    _audit(
        session,
        context,
        action="fund.create",
        resource_type="fund",
        resource_id=fund.id,
    )
    _commit(session, "Fund name or product code already exists")
    return {"data": _fund_data(fund)}


@router.patch("/funds/{fund_id}")
def update_fund(
    fund_id: int,
    payload: FundUpdate,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    fund = _fund_or_404(session, fund_id)
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")
    if values.get("standard_name") is None and "standard_name" in values:
        raise HTTPException(status_code=422, detail="standard_name cannot be cleared")
    if "standard_name" in values:
        _assert_fund_name_available(
            session, values["standard_name"], exclude_fund_id=fund.id
        )
    if "product_code" in values:
        _assert_product_code_available(
            session, values["product_code"], exclude_fund_id=fund.id
        )
    for field, value in values.items():
        setattr(fund, field, value)
    _audit(
        session,
        context,
        action="fund.update",
        resource_type="fund",
        resource_id=fund.id,
        summary={"fields": sorted(values)},
    )
    _commit(session, "Fund name or product code already exists")
    return {"data": _fund_data(fund)}


@router.post("/funds/{fund_id}/enable")
def enable_fund(
    fund_id: int, context: CatalogOperator, session: DatabaseSession
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    lock_company_scope(session)
    fund = _fund_or_404(session, fund_id)
    if fund.status != FundStatus.ACTIVE:
        fund.status = FundStatus.ACTIVE
        queue_company_analysis_run(
            session, fund_id=fund.id, actor_user_id=context.user.id
        )
    _audit(
        session,
        context,
        action="fund.enable",
        resource_type="fund",
        resource_id=fund.id,
    )
    _commit(session)
    return {"data": _fund_data(fund)}


@router.post("/funds/{fund_id}/disable")
def disable_fund(
    fund_id: int,
    payload: ReasonRequest,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_fund_access(session, context.user, fund_id)
    lock_company_scope(session)
    fund = _fund_or_404(session, fund_id)
    if fund.status != FundStatus.INACTIVE:
        fund.status = FundStatus.INACTIVE
        queue_company_analysis_run(
            session, fund_id=fund.id, actor_user_id=context.user.id
        )
    _audit(
        session,
        context,
        action="fund.disable",
        resource_type="fund",
        resource_id=fund.id,
        reason=payload.reason,
    )
    _commit(session)
    return {"data": _fund_data(fund)}


@router.get("/funds/{fund_id}/deletion-impact")
def fund_deletion_impact(
    fund_id: int, context: CatalogAdmin, session: DatabaseSession
) -> dict[str, object]:
    """Preview how many rows deleting a fund would destroy, per table."""
    require_fund_access(session, context.user, fund_id)

    fund = _fund_or_404(session, fund_id)
    impact = collect_fund_removal_impact(session, fund_id)
    if impact is None:  # pragma: no cover - _fund_or_404 already guarantees this
        raise HTTPException(status_code=404, detail="Fund not found")
    return {"data": {"fund": _fund_data(fund), **impact}}


@router.delete("/funds/{fund_id}")
def delete_fund(
    fund_id: int,
    payload: ReasonRequest,
    context: CatalogAdmin,
    session: DatabaseSession,
) -> dict[str, object]:
    """Permanently erase a fund and its dependent rows.

    Only already-disabled funds can be deleted. Disabling first keeps this
    irreversible step behind a deliberate two-stage action, and it also means
    the fund is already absent from every dashboard, so removal cannot change
    what users currently see.
    """
    require_fund_access(session, context.user, fund_id)

    lock_company_scope(session)
    fund = _fund_or_404(session, fund_id)
    if fund.status != FundStatus.INACTIVE:
        raise HTTPException(
            status_code=409, detail="Fund must be disabled before deletion"
        )
    snapshot = _fund_data(fund)
    impact = remove_fund(session, fund_id)
    if impact is None:  # pragma: no cover - _fund_or_404 already guarantees this
        raise HTTPException(status_code=404, detail="Fund not found")
    _audit(
        session,
        context,
        action="fund.delete",
        resource_type="fund",
        resource_id=fund_id,
        reason=payload.reason,
        summary={
            "standard_name": snapshot["standard_name"],
            "product_code": snapshot["product_code"],
            "total_removed": impact["total_removed"],
            "detached_analysis_runs": impact["detached_analysis_runs"],
            "preserved_company_metrics": impact["preserved_company_metrics"],
        },
    )
    _commit(session)
    return {"data": {"id": fund_id, "deleted": True, "fund": snapshot, **impact}}
