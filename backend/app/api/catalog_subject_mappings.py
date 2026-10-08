"""Subject-mapping maintenance routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import Field, model_validator
from sqlalchemy import func, select

from app.api.catalog_shared import (
    CatalogOperator,
    DatabaseSession,
    StrictModel,
    _audit,
    _commit,
    _flush,
    _mapping_data,
    _mapping_or_404,
    _optional_text,
    _validate_mapping_fields,
    _validate_mapping_request,
)
from app.auth.fund_access import require_unrestricted_access
from app.db.base import MappingStatus
from app.db.models import SubjectMapping

router = APIRouter(tags=["catalog"])


class SubjectMappingCreate(StrictModel):
    subject_code_or_prefix: str | None = Field(default=None, max_length=100)
    raw_name_pattern: str | None = Field(default=None, max_length=255)
    standard_category: str = Field(min_length=1, max_length=100)
    is_leaf: bool = True
    include_in_holdings: bool = False
    valid_from: date | None = None
    valid_to: date | None = None
    rule_version: str = Field(min_length=1, max_length=50)
    status: MappingStatus = MappingStatus.ACTIVE

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for field in (
            "subject_code_or_prefix",
            "raw_name_pattern",
            "standard_category",
            "rule_version",
        ):
            if isinstance(result.get(field), str):
                result[field] = _optional_text(result[field])
        return result

    @model_validator(mode="after")
    def validate_rule(self) -> SubjectMappingCreate:
        _validate_mapping_fields(
            self.subject_code_or_prefix,
            self.raw_name_pattern,
            self.valid_from,
            self.valid_to,
        )
        if not self.standard_category.strip() or not self.rule_version.strip():
            raise ValueError("standard_category and rule_version must not be blank")
        return self


class SubjectMappingUpdate(StrictModel):
    subject_code_or_prefix: str | None = Field(default=None, max_length=100)
    raw_name_pattern: str | None = Field(default=None, max_length=255)
    standard_category: str | None = Field(default=None, max_length=100)
    is_leaf: bool | None = None
    include_in_holdings: bool | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    rule_version: str | None = Field(default=None, max_length=50)
    status: MappingStatus | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_values(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for field in (
            "subject_code_or_prefix",
            "raw_name_pattern",
            "standard_category",
            "rule_version",
        ):
            if isinstance(result.get(field), str):
                result[field] = _optional_text(result[field])
        return result


class OptionalReasonRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def trim_reason(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if isinstance(result.get("reason"), str):
            result["reason"] = _optional_text(result["reason"])
        return result


@router.get("/subjects/mappings")
def list_subject_mappings(
    context: CatalogOperator,
    session: DatabaseSession,
    mapping_status: MappingStatus | None = Query(default=None, alias="status"),  # noqa: B008
    category: str | None = Query(default=None, max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    statement = select(SubjectMapping).order_by(SubjectMapping.id)
    count_statement = select(func.count(SubjectMapping.id))
    if mapping_status is not None:
        statement = statement.where(SubjectMapping.status == mapping_status)
        count_statement = count_statement.where(SubjectMapping.status == mapping_status)
    if category:
        normalized_category = category.strip()
        statement = statement.where(
            SubjectMapping.standard_category == normalized_category
        )
        count_statement = count_statement.where(
            SubjectMapping.standard_category == normalized_category
        )
    offset = (page - 1) * page_size
    mappings = session.scalars(statement.offset(offset).limit(page_size)).all()
    total = session.scalar(count_statement) or 0
    return {
        "data": [_mapping_data(item) for item in mappings],
        "meta": {"page": page, "page_size": page_size, "total": total},
    }


@router.post("/subjects/mappings", status_code=status.HTTP_201_CREATED)
def create_subject_mapping(
    payload: SubjectMappingCreate,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    mapping = SubjectMapping(
        subject_code_or_prefix=payload.subject_code_or_prefix,
        raw_name_pattern=payload.raw_name_pattern,
        standard_category=payload.standard_category,
        is_leaf=payload.is_leaf,
        include_in_holdings=payload.include_in_holdings,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        rule_version=payload.rule_version,
        status=payload.status,
    )
    session.add(mapping)
    _flush(session)
    _audit(
        session,
        context,
        action="subject_mapping.create",
        resource_type="subject_mapping",
        resource_id=mapping.id,
    )
    _commit(session)
    return {"data": _mapping_data(mapping)}


@router.patch("/subjects/mappings/{mapping_id}")
def update_subject_mapping(
    mapping_id: int,
    payload: SubjectMappingUpdate,
    context: CatalogOperator,
    session: DatabaseSession,
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    mapping = _mapping_or_404(session, mapping_id)
    values = payload.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")
    _validate_mapping_request(
        values.get("subject_code_or_prefix", mapping.subject_code_or_prefix),
        values.get("raw_name_pattern", mapping.raw_name_pattern),
        values.get("valid_from", mapping.valid_from),
        values.get("valid_to", mapping.valid_to),
    )
    if "standard_category" in values and not values["standard_category"]:
        raise HTTPException(
            status_code=422, detail="standard_category must not be blank"
        )
    if "rule_version" in values and not values["rule_version"]:
        raise HTTPException(status_code=422, detail="rule_version must not be blank")
    for field, value in values.items():
        setattr(mapping, field, value)
    _audit(
        session,
        context,
        action="subject_mapping.update",
        resource_type="subject_mapping",
        resource_id=mapping.id,
        summary={"fields": sorted(values)},
    )
    _commit(session)
    return {"data": _mapping_data(mapping)}


@router.post("/subjects/mappings/{mapping_id}/disable")
def disable_subject_mapping(
    mapping_id: int,
    context: CatalogOperator,
    session: DatabaseSession,
    payload: OptionalReasonRequest | None = None,
) -> dict[str, object]:
    require_unrestricted_access(session, context.user)
    mapping = _mapping_or_404(session, mapping_id)
    mapping.status = MappingStatus.INACTIVE
    _audit(
        session,
        context,
        action="subject_mapping.disable",
        resource_type="subject_mapping",
        resource_id=mapping.id,
        reason=payload.reason if payload else None,
    )
    _commit(session)
    return {"data": _mapping_data(mapping)}
