"""Shared SQLAlchemy declarative base and database column helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return an aware UTC timestamp for ORM-side defaults."""

    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all database models."""


class FundStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class MappingStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ParserRuleStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class SourceType(StrEnum):
    UPLOAD = "upload"
    EMAIL = "email"
    MIGRATION = "migration"
    OTHER = "other"


class ImportBatchStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ValuationStatus(StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    VALIDATING = "validating"
    PUBLISHABLE = "publishable"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    NON_VALUATION = "non_valuation"
    REVOKED = "revoked"


class ValidationLevel(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AnalysisRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RiskSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RiskEventStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"
    BOSS = "boss"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class FundAccessMode(StrEnum):
    ALL = "all"
    SELECTED = "selected"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_DUE = "retry_due"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def enum_column(enum_type: type[StrEnum], **kwargs: Any) -> Mapped[str]:
    """Build a portable constrained string enum column."""

    return mapped_column(
        SAEnum(
            enum_type,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            name=f"ck_{enum_type.__name__.lower()}",
        ),
        **kwargs,
    )


def created_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
