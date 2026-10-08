"""Database orchestration for valuation validation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.base import AuditResult, ValidationLevel, ValuationStatus
from app.db.models import (
    AuditLog,
    FundDailySnapshot,
    PositionDaily,
    ShareClassDailySnapshot,
    ValidationResult,
    ValuationVersion,
)
from app.parser.interface import ParsedValuation

from .rules import (
    ToleranceConfig,
    ValidationReport,
    validate_parsed_valuation,
    validate_values,
)


class ValidationServiceError(RuntimeError):
    """Stable, non-database error exposed by the validation orchestration layer."""


class ValidationVersionNotFound(ValidationServiceError):
    """The requested valuation version does not exist."""


class ValidationStateError(ValidationServiceError):
    """The version cannot be validated in its current lifecycle state."""


VALIDATABLE_VERSION_STATUSES = {
    ValuationStatus.RECEIVED,
    ValuationStatus.PARSING,
    ValuationStatus.VALIDATING,
    ValuationStatus.PUBLISHABLE,
    ValuationStatus.PENDING_REVIEW,
}


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """Small persistence-independent summary for callers that need counts."""

    version_id: int
    report: ValidationReport


class ValidationService:
    """Run pure rules and persist their result as one database operation."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def validate_version(
        self,
        version_id: int,
        *,
        parsed: ParsedValuation | None = None,
        actor_user_id: int | None = None,
        tolerances: ToleranceConfig | None = None,
    ) -> ValidationReport:
        """Validate one unpublished version and update its workflow status.

        ``parsed`` is useful while the parser-to-standardization pipeline is
        being wired.  When omitted, normalized snapshot rows already stored for
        the version are used.
        """

        try:
            with self.session.begin_nested():
                version = self._load_version(version_id)
                self._ensure_validatable(version)
                previous_status = ValuationStatus(version.status)
                version.status = ValuationStatus.VALIDATING
                self.session.flush()

                if parsed is not None:
                    report = validate_parsed_valuation(parsed, tolerances=tolerances)
                else:
                    report = self._validate_stored_values(
                        version_id, tolerances=tolerances
                    )
                self.session.execute(
                    delete(ValidationResult).where(
                        ValidationResult.valuation_version_id == version_id
                    )
                )
                self.session.add_all(
                    ValidationResult(
                        valuation_version_id=version_id,
                        rule_code=finding.rule_code,
                        level=finding.level,
                        actual_value=finding.actual_value,
                        expected_value=finding.expected_value,
                        difference=finding.difference,
                        source_location=finding.source_location,
                        message=finding.message,
                    )
                    for finding in report.findings
                )
                version.status = report.status
                self.session.add(
                    AuditLog(
                        actor_user_id=actor_user_id,
                        action="valuation.validation_completed",
                        resource_type="valuation_version",
                        resource_id=str(version_id),
                        summary={
                            "from_status": previous_status.value,
                            "to_status": report.status.value,
                            "critical_count": report.critical_count,
                            "warning_count": report.warning_count,
                            "info_count": report.info_count,
                        },
                        result=AuditResult.SUCCESS,
                    )
                )
                self.session.flush()
                return report
        except ValidationServiceError:
            raise
        except SQLAlchemyError as exc:
            raise ValidationServiceError("validation_persistence_failed") from exc

    def validate(
        self,
        version_id: int,
        *,
        parsed: ParsedValuation | None = None,
        actor_user_id: int | None = None,
        tolerances: ToleranceConfig | None = None,
    ) -> ValidationReport:
        """Short alias for callers that treat the service as a use case."""

        return self.validate_version(
            version_id,
            parsed=parsed,
            actor_user_id=actor_user_id,
            tolerances=tolerances,
        )

    def summary(
        self,
        version_id: int,
        *,
        parsed: ParsedValuation | None = None,
        actor_user_id: int | None = None,
        tolerances: ToleranceConfig | None = None,
    ) -> ValidationSummary:
        """Validate and return the version id together with the report."""

        return ValidationSummary(
            version_id=version_id,
            report=self.validate_version(
                version_id,
                parsed=parsed,
                actor_user_id=actor_user_id,
                tolerances=tolerances,
            ),
        )

    def _load_version(self, version_id: int) -> ValuationVersion:
        statement = select(ValuationVersion).where(ValuationVersion.id == version_id)
        if self._supports_row_locks:
            statement = statement.with_for_update()
        version = self.session.scalar(statement)
        if version is None:
            raise ValidationVersionNotFound("valuation_version_not_found")
        return version

    @staticmethod
    def _ensure_validatable(version: ValuationVersion) -> None:
        status = ValuationStatus(version.status)
        if status in {
            ValuationStatus.PUBLISHED,
            ValuationStatus.SUPERSEDED,
            ValuationStatus.REVOKED,
        }:
            raise ValidationStateError("published_version_is_immutable")
        if status == ValuationStatus.REJECTED:
            raise ValidationStateError("rejected_version_is_immutable")
        if status not in VALIDATABLE_VERSION_STATUSES:
            raise ValidationStateError(f"invalid_status_for_validation:{status.value}")

    @property
    def _supports_row_locks(self) -> bool:
        return (
            self.session.bind is not None
            and self.session.bind.dialect.name == "postgresql"
        )

    def _validate_stored_values(
        self,
        version_id: int,
        *,
        tolerances: ToleranceConfig | None,
    ) -> ValidationReport:
        snapshot = self.session.scalar(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id == version_id
            )
        )
        share_classes = self.session.scalars(
            select(ShareClassDailySnapshot)
            .where(ShareClassDailySnapshot.valuation_version_id == version_id)
            .order_by(ShareClassDailySnapshot.id)
        ).all()
        positions = self.session.scalars(
            select(PositionDaily)
            .where(PositionDaily.valuation_version_id == version_id)
            .order_by(PositionDaily.id)
        ).all()
        if snapshot is None:
            report = validate_values(
                total_assets=None,
                total_liabilities=None,
                net_asset_value=None,
                unit_nav=None,
                previous_unit_nav=None,
                daily_return=None,
                share_classes=share_classes,
                positions=positions,
                tolerances=tolerances,
            )
            return _prepend_missing_snapshot(report)
        return validate_values(
            total_assets=snapshot.total_assets,
            total_liabilities=snapshot.total_liabilities,
            net_asset_value=snapshot.net_asset_value,
            unit_nav=snapshot.unit_nav,
            previous_unit_nav=snapshot.previous_unit_nav,
            daily_return=snapshot.daily_return,
            share_classes=share_classes,
            positions=positions,
            tolerances=tolerances,
        )


def _prepend_missing_snapshot(report: ValidationReport) -> ValidationReport:
    from .rules import ValidationFinding

    # A missing product snapshot means the stored valuation is incomplete. Do
    # not allow it to be published until the snapshot is restored and the
    # version is validated again; otherwise analytics' inner join would
    # silently omit this valuation from the published data set.
    finding = ValidationFinding(
        rule_code="valuation_snapshot_missing",
        level=ValidationLevel.CRITICAL,
        source_location="fund_daily_snapshot",
        message="估值版本缺少产品日快照，补齐后才能发布",
    )
    findings = (finding, *report.findings)
    return ValidationReport(findings=findings, status=ValuationStatus.PENDING_REVIEW)


__all__ = [
    "ValidationService",
    "ValidationServiceError",
    "ValidationStateError",
    "ValidationSummary",
    "ValidationVersionNotFound",
]
