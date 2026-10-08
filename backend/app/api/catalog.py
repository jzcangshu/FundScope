"""Aggregated product master-data and subject-mapping routes."""

from fastapi import APIRouter

from .catalog_aliases import (
    AliasInput,
    AliasUpdate,
    create_alias,
    delete_alias,
    list_aliases,
    update_alias,
)
from .catalog_aliases import (
    router as aliases_router,
)
from .catalog_funds import (
    FundCreate,
    FundUpdate,
    ReasonRequest,
    create_fund,
    delete_fund,
    disable_fund,
    enable_fund,
    fund_deletion_impact,
    update_fund,
)
from .catalog_funds import (
    router as funds_router,
)
from .catalog_share_classes import (
    ShareClassCreate,
    ShareClassDisableRequest,
    ShareClassUpdate,
    create_share_class,
    disable_share_class,
    enable_share_class,
    list_share_classes,
    update_share_class,
)
from .catalog_share_classes import (
    router as share_classes_router,
)
from .catalog_shared import (
    CatalogAdmin,
    CatalogOperator,
    DatabaseSession,
    StrictModel,
    _alias_data,
    _alias_or_404,
    _assert_alias_available,
    _assert_fund_name_available,
    _assert_product_code_available,
    _assert_share_code_available,
    _audit,
    _commit,
    _flush,
    _fund_data,
    _fund_or_404,
    _mapping_data,
    _mapping_or_404,
    _normalized,
    _optional_text,
    _required_text,
    _share_class_data,
    _share_class_or_404,
    _validate_date_range,
    _validate_date_range_request,
    _validate_mapping_fields,
    _validate_mapping_request,
)
from .catalog_subject_mappings import (
    OptionalReasonRequest,
    SubjectMappingCreate,
    SubjectMappingUpdate,
    create_subject_mapping,
    disable_subject_mapping,
    list_subject_mappings,
    update_subject_mapping,
)
from .catalog_subject_mappings import (
    router as subject_mappings_router,
)

__all__ = [
    "AliasInput",
    "AliasUpdate",
    "CatalogAdmin",
    "CatalogOperator",
    "DatabaseSession",
    "FundCreate",
    "FundUpdate",
    "OptionalReasonRequest",
    "ReasonRequest",
    "ShareClassCreate",
    "ShareClassDisableRequest",
    "ShareClassUpdate",
    "StrictModel",
    "SubjectMappingCreate",
    "SubjectMappingUpdate",
    "_alias_data",
    "_alias_or_404",
    "_assert_alias_available",
    "_assert_fund_name_available",
    "_assert_product_code_available",
    "_assert_share_code_available",
    "_audit",
    "_commit",
    "_flush",
    "_fund_data",
    "_fund_or_404",
    "_mapping_data",
    "_mapping_or_404",
    "_normalized",
    "_optional_text",
    "_required_text",
    "_share_class_data",
    "_share_class_or_404",
    "_validate_date_range",
    "_validate_date_range_request",
    "_validate_mapping_fields",
    "_validate_mapping_request",
    "create_alias",
    "create_fund",
    "create_share_class",
    "create_subject_mapping",
    "delete_alias",
    "delete_fund",
    "disable_fund",
    "disable_share_class",
    "disable_subject_mapping",
    "enable_fund",
    "enable_share_class",
    "fund_deletion_impact",
    "list_aliases",
    "list_share_classes",
    "list_subject_mappings",
    "router",
    "update_alias",
    "update_fund",
    "update_share_class",
    "update_subject_mapping",
]

router = APIRouter(prefix="/api/v1", tags=["catalog"])
router.include_router(funds_router)
router.include_router(aliases_router)
router.include_router(share_classes_router)
router.include_router(subject_mappings_router)
