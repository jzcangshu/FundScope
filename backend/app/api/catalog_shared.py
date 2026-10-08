"""Shared dependencies, validation, serialization, and persistence helpers for catalog routes."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import AuthContext, get_db, require_roles
from app.auth.service import AuthService
from app.db.base import UserRole
from app.db.models import Fund, FundAlias, ShareClass, SubjectMapping

DatabaseSession = Annotated[Session, Depends(get_db)]
CatalogOperator = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
]
# Destructive catalog changes are administrator-only; operators may still
# enable/disable a fund, but only an admin can erase one.
CatalogAdmin = Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN))]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _required_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _validate_date_range(
    valid_from: date | None, valid_to: date | None, resource: str
) -> None:
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        raise ValueError(f"{resource} valid_from must not be after valid_to")


def _normalized(value: str) -> str:
    return value.strip().casefold()


def _validate_mapping_fields(
    code_or_prefix: str | None,
    raw_name_pattern: str | None,
    valid_from: date | None,
    valid_to: date | None,
) -> None:
    has_code = bool(code_or_prefix and code_or_prefix.strip())
    has_pattern = bool(raw_name_pattern and raw_name_pattern.strip())
    if not has_code and not has_pattern:
        raise ValueError("subject_code_or_prefix or raw_name_pattern is required")
    # Each side of the OR must independently carry content. A row with
    # code_or_prefix="" but raw_name_pattern="foo" would otherwise be saved
    # by the API and silently dropped by the matcher's truthiness guard,
    # producing dead rules.
    if code_or_prefix is not None and not has_code:
        raise ValueError("subject_code_or_prefix must not be blank")
    if raw_name_pattern is not None and not has_pattern:
        raise ValueError("raw_name_pattern must not be blank")
    _validate_date_range(valid_from, valid_to, "mapping")


def _validate_date_range_request(
    valid_from: date | None, valid_to: date | None, resource: str
) -> None:
    try:
        _validate_date_range(valid_from, valid_to, resource)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date range") from exc


def _validate_mapping_request(
    code_or_prefix: str | None,
    raw_name_pattern: str | None,
    valid_from: date | None,
    valid_to: date | None,
) -> None:
    try:
        _validate_mapping_fields(code_or_prefix, raw_name_pattern, valid_from, valid_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid subject mapping") from exc


def _fund_data(fund: Fund) -> dict[str, object]:
    return {
        "id": fund.id,
        "standard_name": fund.standard_name,
        "product_code": fund.product_code,
        "establishment_date": fund.establishment_date,
        "strategy": fund.strategy,
        "manager": fund.manager,
        "status": fund.status,
        "notes": fund.notes,
    }


def _alias_data(alias: FundAlias) -> dict[str, object]:
    return {
        "id": alias.id,
        "fund_id": alias.fund_id,
        "alias": alias.alias,
        "source_location": alias.source_location,
        "match_priority": alias.match_priority,
        "valid_from": alias.valid_from,
        "valid_to": alias.valid_to,
    }


def _share_class_data(share_class: ShareClass) -> dict[str, object]:
    return {
        "id": share_class.id,
        "fund_id": share_class.fund_id,
        "share_code": share_class.share_code,
        "share_name": share_class.share_name,
        "status": "inactive" if share_class.disabled_from else "active",
        "enabled_from": share_class.enabled_from,
        "disabled_from": share_class.disabled_from,
        "notes": share_class.notes,
    }


def _mapping_data(mapping: SubjectMapping) -> dict[str, object]:
    return {
        "id": mapping.id,
        "subject_code_or_prefix": mapping.subject_code_or_prefix,
        "raw_name_pattern": mapping.raw_name_pattern,
        "standard_category": mapping.standard_category,
        "is_leaf": mapping.is_leaf,
        "include_in_holdings": mapping.include_in_holdings,
        "valid_from": mapping.valid_from,
        "valid_to": mapping.valid_to,
        "rule_version": mapping.rule_version,
        "status": mapping.status,
    }


def _fund_or_404(session: Session, fund_id: int) -> Fund:
    fund = session.get(Fund, fund_id)
    if fund is None:
        raise HTTPException(status_code=404, detail="Fund not found")
    return fund


def _alias_or_404(session: Session, fund_id: int, alias_id: int) -> FundAlias:
    alias = session.scalar(
        select(FundAlias).where(FundAlias.id == alias_id, FundAlias.fund_id == fund_id)
    )
    if alias is None:
        raise HTTPException(status_code=404, detail="Alias not found")
    return alias


def _share_class_or_404(
    session: Session, fund_id: int, share_class_id: int
) -> ShareClass:
    share_class = session.scalar(
        select(ShareClass).where(
            ShareClass.id == share_class_id, ShareClass.fund_id == fund_id
        )
    )
    if share_class is None:
        raise HTTPException(status_code=404, detail="Share class not found")
    return share_class


def _mapping_or_404(session: Session, mapping_id: int) -> SubjectMapping:
    mapping = session.get(SubjectMapping, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Subject mapping not found")
    return mapping


def _commit(
    session: Session, detail: str = "Catalog data conflicts with an existing record"
) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


def _flush(
    session: Session, detail: str = "Catalog data conflicts with an existing record"
) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=detail) from exc


def _assert_fund_name_available(
    session: Session, name: str, *, exclude_fund_id: int | None = None
) -> None:
    normalized_name = name.strip().casefold()
    fund_statement = select(Fund).where(
        func.lower(func.trim(Fund.standard_name)) == normalized_name
    )
    if exclude_fund_id is not None:
        fund_statement = fund_statement.where(Fund.id != exclude_fund_id)
    if session.scalar(fund_statement.with_only_columns(Fund.id).limit(1)) is not None:
        raise HTTPException(status_code=409, detail="Fund name already exists")
    alias_statement = select(FundAlias)
    if exclude_fund_id is not None:
        alias_statement = alias_statement.where(FundAlias.fund_id != exclude_fund_id)
    alias_statement = alias_statement.where(
        func.lower(func.trim(FundAlias.alias)) == normalized_name
    )
    if (
        session.scalar(alias_statement.with_only_columns(FundAlias.id).limit(1))
        is not None
    ):
        raise HTTPException(status_code=409, detail="Fund name conflicts with an alias")


def _assert_product_code_available(
    session: Session, product_code: str | None, *, exclude_fund_id: int | None = None
) -> None:
    if product_code is None:
        return
    normalized_code = product_code.strip().casefold()
    if any(
        fund.id != exclude_fund_id
        and fund.product_code is not None
        and fund.product_code.strip().casefold() == normalized_code
        for fund in session.scalars(select(Fund))
    ):
        raise HTTPException(status_code=409, detail="Product code already exists")


def _assert_alias_available(
    session: Session,
    alias: str,
    fund_id: int,
    *,
    exclude_alias_id: int | None = None,
) -> None:
    normalized_alias = alias.strip().casefold()
    fund = session.get(Fund, fund_id)
    if fund is not None and fund.standard_name.strip().casefold() == normalized_alias:
        raise HTTPException(status_code=409, detail="Alias must differ from fund name")
    if any(
        item.id != fund_id and item.standard_name.strip().casefold() == normalized_alias
        for item in session.scalars(select(Fund))
    ):
        raise HTTPException(status_code=409, detail="Alias conflicts with a fund name")
    if any(
        item.id != exclude_alias_id
        and item.alias.strip().casefold() == normalized_alias
        for item in session.scalars(select(FundAlias))
    ):
        raise HTTPException(status_code=409, detail="Alias already exists")


def _assert_share_code_available(
    session: Session,
    fund_id: int,
    share_code: str,
    *,
    exclude_share_class_id: int | None = None,
) -> None:
    normalized_code = share_code.strip().casefold()
    if any(
        item.id != exclude_share_class_id
        and item.share_code.strip().casefold() == normalized_code
        for item in session.scalars(
            select(ShareClass).where(ShareClass.fund_id == fund_id)
        )
    ):
        raise HTTPException(status_code=409, detail="Share code already exists")


def _audit(
    session: Session,
    context: AuthContext,
    *,
    action: str,
    resource_type: str,
    resource_id: int,
    reason: str | None = None,
    summary: dict[str, object] | None = None,
) -> None:
    AuthService(session).record_audit(
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        actor_user_id=context.user.id,
        reason=reason,
        summary=summary,
    )
