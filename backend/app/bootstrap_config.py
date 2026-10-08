"""Validated, non-secret configuration for production bootstrap."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from app.db.base import FundStatus, MappingStatus, RiskSeverity
from app.system.settings import validate_updates

CONFIG_VERSION = 1
FORBIDDEN_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "authorization",
)
SUPPORTED_RULE_TYPES = {
    "daily_return",
    "max_drawdown",
    "current_drawdown",
    "single_position_weight",
    "top_five_weight",
    "concentration",
}


class BootstrapError(ValueError):
    """Raised for unsafe or invalid bootstrap input."""


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must not be blank")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("must be a string")
    return value.strip() or None


def _parse_date(value: object) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError("must be an ISO date")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError("must be an ISO date") from None


class PreflightConfig(ConfigModel):
    storage_root: Path
    migration_manifest: Path

    @field_validator("storage_root", "migration_manifest", mode="before")
    @classmethod
    def absolute_path(cls, value: object) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path is required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("path must be absolute")
        return path


class AliasConfig(ConfigModel):
    alias: StrictStr = Field(min_length=1, max_length=255)
    source_location: StrictStr | None = Field(default=None, max_length=255)
    match_priority: StrictInt = Field(default=0, ge=0, le=1000)
    valid_from: date | None = None
    valid_to: date | None = None

    _trim_alias = field_validator("alias", mode="before")(_required_text)
    _trim_source = field_validator("source_location", mode="before")(_optional_text)
    _parse_dates = field_validator("valid_from", "valid_to", mode="before")(_parse_date)

    @model_validator(mode="after")
    def valid_period(self) -> AliasConfig:
        _date_range(self.valid_from, self.valid_to)
        return self


class ShareClassConfig(ConfigModel):
    share_code: StrictStr = Field(min_length=1, max_length=100)
    share_name: StrictStr = Field(min_length=1, max_length=255)
    enabled_from: date | None = None
    disabled_from: date | None = None
    notes: StrictStr | None = None

    _trim_code = field_validator("share_code", mode="before")(_required_text)
    _trim_name = field_validator("share_name", mode="before")(_required_text)
    _trim_notes = field_validator("notes", mode="before")(_optional_text)
    _parse_dates = field_validator("enabled_from", "disabled_from", mode="before")(
        _parse_date
    )

    @model_validator(mode="after")
    def valid_period(self) -> ShareClassConfig:
        if (
            self.enabled_from
            and self.disabled_from
            and self.disabled_from < self.enabled_from
        ):
            raise ValueError("invalid date range")
        return self


class ProductConfig(ConfigModel):
    standard_name: StrictStr = Field(min_length=1, max_length=255)
    product_code: StrictStr | None = Field(default=None, max_length=100)
    establishment_date: date | None = None
    strategy: StrictStr | None = Field(default=None, max_length=255)
    manager: StrictStr | None = Field(default=None, max_length=255)
    notes: StrictStr | None = None
    status: FundStatus = FundStatus.ACTIVE
    aliases: list[AliasConfig] = Field(min_length=1, max_length=100)
    share_classes: list[ShareClassConfig] = Field(default_factory=list, max_length=100)

    _trim_name = field_validator("standard_name", mode="before")(_required_text)
    _trim_optional = field_validator(
        "product_code", "strategy", "manager", "notes", mode="before"
    )(_optional_text)
    _parse_date = field_validator("establishment_date", mode="before")(_parse_date)


class SubjectMappingConfig(ConfigModel):
    subject_code_or_prefix: StrictStr | None = Field(default=None, max_length=100)
    raw_name_pattern: StrictStr | None = Field(default=None, max_length=255)
    standard_category: StrictStr = Field(min_length=1, max_length=100)
    is_leaf: StrictBool = True
    include_in_holdings: StrictBool = False
    valid_from: date | None = None
    valid_to: date | None = None
    rule_version: StrictStr = Field(min_length=1, max_length=50)
    status: MappingStatus = MappingStatus.ACTIVE

    _trim_match_fields = field_validator(
        "subject_code_or_prefix", "raw_name_pattern", mode="before"
    )(_optional_text)
    _trim_category = field_validator("standard_category", mode="before")(_required_text)
    _trim_version = field_validator("rule_version", mode="before")(_required_text)
    _parse_dates = field_validator("valid_from", "valid_to", mode="before")(_parse_date)

    @model_validator(mode="after")
    def valid_mapping(self) -> SubjectMappingConfig:
        if not self.subject_code_or_prefix and not self.raw_name_pattern:
            raise ValueError("a code or name pattern is required")
        _date_range(self.valid_from, self.valid_to)
        return self


class RiskRuleConfig(ConfigModel):
    rule_code: StrictStr = Field(min_length=1, max_length=100)
    rule_type: StrictStr = Field(min_length=1, max_length=100)
    scope: StrictStr = Field(default="all", min_length=1, max_length=100)
    threshold: Decimal
    severity: RiskSeverity = RiskSeverity.WARNING
    valid_from: date | None = None
    valid_to: date | None = None
    version: StrictStr = Field(default="1", max_length=50)
    enabled: StrictBool = True

    _trim_text = field_validator(
        "rule_code", "rule_type", "scope", "version", mode="before"
    )(_required_text)
    _parse_dates = field_validator("valid_from", "valid_to", mode="before")(_parse_date)

    @field_validator("threshold")
    @classmethod
    def finite_threshold(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("threshold must be finite")
        return value

    @field_validator("rule_type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        if value not in SUPPORTED_RULE_TYPES:
            raise ValueError("unsupported risk rule type")
        return value

    @model_validator(mode="after")
    def valid_period(self) -> RiskRuleConfig:
        _date_range(self.valid_from, self.valid_to)
        return self


class BootstrapConfig(ConfigModel):
    version: StrictInt = CONFIG_VERSION
    preflight: PreflightConfig
    products: list[ProductConfig] = Field(default_factory=list, max_length=100)
    subject_mappings: list[SubjectMappingConfig] = Field(
        default_factory=list, max_length=10000
    )
    risk_rules: list[RiskRuleConfig] = Field(default_factory=list, max_length=1000)
    system_settings: dict[str, object] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != CONFIG_VERSION:
            raise ValueError("unsupported bootstrap config version")
        return value

    @field_validator("system_settings")
    @classmethod
    def valid_settings(cls, value: dict[str, object]) -> dict[str, object]:
        try:
            return validate_updates(value)
        except ValueError:
            raise ValueError("invalid system setting") from None

    @model_validator(mode="after")
    def unique_entries(self) -> BootstrapConfig:
        _reject_duplicate_config(self.products, self.subject_mappings, self.risk_rules)
        return self

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def load_config(path: Path) -> BootstrapConfig:
    """Read and strictly validate a JSON bootstrap configuration."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(
            f"unable to read bootstrap config: {type(exc).__name__}"
        ) from None
    return validate_config(payload)


def validate_config(payload: object) -> BootstrapConfig:
    """Validate untrusted JSON without echoing supplied values in errors."""

    root = _object(payload, "configuration")
    _reject_forbidden_fields(root)
    try:
        return BootstrapConfig.model_validate(root)
    except ValidationError:
        raise BootstrapError("invalid bootstrap configuration") from None


def _reject_duplicate_config(
    products: Sequence[ProductConfig],
    mappings: Sequence[SubjectMappingConfig],
    risk_rules: Sequence[RiskRuleConfig],
) -> None:
    names = [_normalize(product.standard_name) for product in products]
    if len(names) != len(set(names)):
        raise BootstrapError("duplicate product in configuration")
    codes = [
        _normalize(product.product_code)
        for product in products
        if product.product_code is not None
    ]
    if len(codes) != len(set(codes)):
        raise BootstrapError("duplicate product code in configuration")
    for product in products:
        aliases = [_normalize(item.alias) for item in product.aliases]
        if len(aliases) != len(set(aliases)):
            raise BootstrapError("duplicate alias in configuration")
        shares = [_normalize(item.share_code) for item in product.share_classes]
        if len(shares) != len(set(shares)):
            raise BootstrapError("duplicate share code in configuration")
    fund_names = set(names)
    aliases = [
        _normalize(alias.alias) for product in products for alias in product.aliases
    ]
    if len(aliases) != len(set(aliases)) or fund_names.intersection(aliases):
        raise BootstrapError("product names and aliases must be globally unique")
    mapping_keys = [
        (
            item.subject_code_or_prefix,
            item.raw_name_pattern,
            item.standard_category,
            item.rule_version,
        )
        for item in mappings
    ]
    if len(mapping_keys) != len(set(mapping_keys)):
        raise BootstrapError("duplicate subject mapping in configuration")
    rule_keys = [(item.rule_code, item.version) for item in risk_rules]
    if len(rule_keys) != len(set(rule_keys)):
        raise BootstrapError("duplicate risk rule in configuration")


def _reject_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BootstrapError("configuration keys must be strings")
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in FORBIDDEN_KEY_PARTS):
                raise BootstrapError("forbidden configuration field")
            _reject_forbidden_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_fields(child)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} must be an object")
    return value


def _date_range(valid_from: date | None, valid_to: date | None) -> None:
    if valid_from and valid_to and valid_to < valid_from:
        raise BootstrapError("invalid date range")


def _normalize(value: str | None) -> str:
    return (value or "").strip().casefold()


__all__ = [
    "AliasConfig",
    "BootstrapConfig",
    "BootstrapError",
    "PreflightConfig",
    "ProductConfig",
    "RiskRuleConfig",
    "ShareClassConfig",
    "SubjectMappingConfig",
    "load_config",
    "validate_config",
]
