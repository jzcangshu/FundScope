"""Transactional review and publication workflow for valuation versions."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.base import (
    AnalysisRunStatus,
    AuditResult,
    ValidationLevel,
    ValuationStatus,
    utcnow,
)
from app.db.models import (
    AccountSubjectDaily,
    AnalysisRun,
    AuditLog,
    BackgroundJob,
    FieldProvenance,
    Fund,
    FundDailySnapshot,
    PositionDaily,
    ShareClassDailySnapshot,
    ValidationResult,
    ValuationVersion,
)


class PublishingServiceError(RuntimeError):
    """Stable domain error that does not expose database implementation details."""


class PublishingStateError(PublishingServiceError):
    """The requested workflow action is invalid for the current version state."""


class PublishingValidationError(PublishingServiceError):
    """Validation or review requirements have not been satisfied."""


class PublishingConflictError(PublishingServiceError):
    """A concurrent publication prevented the requested action."""


class PublishedVersionImmutableError(PublishingServiceError):
    """Released valuation records and their standardized details cannot be edited."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    version_id: int
    fund_id: int
    valuation_date: date
    superseded_version_ids: tuple[int, ...]
    analysis_run_id: int | None
    validation_ignored_count: int = 0


@dataclass(frozen=True, slots=True)
class ReviewResult:
    version_id: int
    status: ValuationStatus


@dataclass(frozen=True, slots=True)
class BatchPublicationResult:
    requested: int
    published: int
    failed: tuple[dict[str, object], ...]
    ignored_findings: int


IMMUTABLE_DETAIL_TYPES = (
    AccountSubjectDaily,
    FieldProvenance,
    FundDailySnapshot,
    PositionDaily,
    ShareClassDailySnapshot,
    ValidationResult,
)
IMMUTABLE_VERSION_STATUSES = {
    ValuationStatus.PUBLISHED,
    ValuationStatus.SUPERSEDED,
    ValuationStatus.REVOKED,
}
RELEASED_VERSION_MUTATION_KEY = "publishing_allowed_released_version_ids"


@event.listens_for(Session, "before_flush")
def _protect_released_version_details(
    session: Session, _flush_context: object, _instances: object
) -> None:
    """Reject ORM updates and deletes of released valuation details."""

    candidates = tuple(session.new) + tuple(session.dirty) + tuple(session.deleted)
    detail_candidates = tuple(
        item for item in candidates if isinstance(item, IMMUTABLE_DETAIL_TYPES)
    )
    version_ids = {
        version_id
        for item in detail_candidates
        if (version_id := getattr(item, "valuation_version_id", None)) is not None
    }
    versions = (
        {
            version.id: version
            for version in session.scalars(
                select(ValuationVersion).where(ValuationVersion.id.in_(version_ids))
            )
        }
        if version_ids
        else {}
    )
    for item in detail_candidates:
        version_id = getattr(item, "valuation_version_id", None)
        if version_id is None:
            continue
        version = versions.get(version_id)
        if (
            version is not None
            and ValuationStatus(version.status) in IMMUTABLE_VERSION_STATUSES
            and version_id not in session.info.get(RELEASED_VERSION_MUTATION_KEY, set())
        ):
            raise PublishedVersionImmutableError(
                "published_version_details_are_immutable"
            )

    for item in tuple(session.dirty) + tuple(session.deleted):
        if not isinstance(item, ValuationVersion):
            continue
        if item.id in session.info.get(RELEASED_VERSION_MUTATION_KEY, set()):
            continue
        if item in session.deleted:
            if _coerce_status(item.status) in IMMUTABLE_VERSION_STATUSES:
                raise PublishedVersionImmutableError(
                    "published_version_parent_is_immutable"
                )
            continue
        state = inspect(item)
        changed_fields = tuple(
            attribute.key
            for attribute in state.mapper.column_attrs
            if state.attrs[attribute.key].history.has_changes()
        )
        if not changed_fields:
            continue
        current_status = _coerce_status(item.status)
        previous_statuses = {
            status
            for status in (
                _coerce_status(value) for value in state.attrs.status.history.deleted
            )
            if status is not None
        }
        if (
            current_status in IMMUTABLE_VERSION_STATUSES
            or previous_statuses & IMMUTABLE_VERSION_STATUSES
        ):
            raise PublishedVersionImmutableError(
                "published_version_parent_is_immutable"
            )


class PublishingService:
    """Own valuation review and publication state changes behind one interface."""

    def __init__(self, session: Session, *, methodology_version: str = "v1") -> None:
        self.session = session
        self.methodology_version = methodology_version

    def pending_reviews(
        self, *, fund_id: int | None = None
    ) -> tuple[ValuationVersion, ...]:
        statement = (
            select(ValuationVersion)
            .where(ValuationVersion.status == ValuationStatus.PENDING_REVIEW)
            .order_by(ValuationVersion.valuation_date, ValuationVersion.id)
        )
        if fund_id is not None:
            statement = statement.where(ValuationVersion.fund_id == fund_id)
        return tuple(self.session.scalars(statement).all())

    def publish_all_publishable(
        self,
        *,
        actor_user_id: int | None,
        actor_label: str | None,
        reason: str,
        allowed_fund_ids: frozenset[int] | None = None,
    ) -> BatchPublicationResult:
        """Publish the current publishable queue, isolating failures per version."""

        normalized_reason = _required_reason(reason, "publication_reason_required")
        statement = (
            select(ValuationVersion.id)
            .where(ValuationVersion.status == ValuationStatus.PUBLISHABLE)
            .order_by(ValuationVersion.valuation_date, ValuationVersion.id)
        )
        if allowed_fund_ids is not None:
            statement = statement.where(ValuationVersion.fund_id.in_(allowed_fund_ids))
        version_ids = tuple(self.session.scalars(statement).all())
        published = 0
        ignored_findings = 0
        failures: list[dict[str, object]] = []
        for version_id in version_ids:
            try:
                result = self.publish_version(
                    version_id,
                    actor_user_id=actor_user_id,
                    actor_label=actor_label,
                    reason=normalized_reason,
                    confirm_warnings=True,
                    ignore_validations=True,
                )
            except PublishingServiceError as exc:
                failures.append({"version_id": version_id, "error": str(exc)})
                continue
            published += 1
            ignored_findings += result.validation_ignored_count
        return BatchPublicationResult(
            requested=len(version_ids),
            published=published,
            failed=tuple(failures),
            ignored_findings=ignored_findings,
        )

    def complete_review(
        self,
        version_id: int,
        *,
        approved: bool,
        actor_user_id: int | None,
        note: str,
    ) -> ReviewResult:
        """Approve a pending review for publication or reject the version."""

        review_note = _required_reason(note, "review_note_required")
        try:
            with self.session.begin_nested():
                version = self._load_version(version_id, for_update=True)
                self._require_status(version, ValuationStatus.PENDING_REVIEW)
                target_status = (
                    ValuationStatus.PUBLISHABLE
                    if approved
                    else ValuationStatus.REJECTED
                )
                version.status = target_status
                if approved:
                    version.release_reason = review_note
                self._audit(
                    action=(
                        "valuation.review_approved"
                        if approved
                        else "valuation.review_rejected"
                    ),
                    version=version,
                    actor_user_id=actor_user_id,
                    reason=review_note,
                    summary={"to_status": target_status.value},
                )
                self.session.flush()
                return ReviewResult(version_id=version.id, status=target_status)
        except PublishingServiceError:
            raise
        except SQLAlchemyError as exc:
            raise PublishingServiceError("review_persistence_failed") from exc

    def acknowledge_review(
        self,
        version_id: int,
        *,
        allow_publish: bool,
        actor_user_id: int | None,
        note: str,
    ) -> ReviewResult:
        """Record the review decision using the API-facing vocabulary."""

        return self.complete_review(
            version_id,
            approved=allow_publish,
            actor_user_id=actor_user_id,
            note=note,
        )

    def reject_version(
        self,
        version_id: int,
        *,
        actor_user_id: int | None,
        reason: str,
    ) -> ReviewResult:
        """Reject an unpublished version that is awaiting a decision."""

        rejection_reason = _required_reason(reason, "rejection_reason_required")
        try:
            with self.session.begin_nested():
                version = self._load_version(version_id, for_update=True)
                if ValuationStatus(version.status) not in {
                    ValuationStatus.PENDING_REVIEW,
                    ValuationStatus.PUBLISHABLE,
                }:
                    raise PublishingStateError("version_cannot_be_rejected")
                version.status = ValuationStatus.REJECTED
                self._audit(
                    action="valuation.rejected",
                    version=version,
                    actor_user_id=actor_user_id,
                    reason=rejection_reason,
                    summary={"to_status": ValuationStatus.REJECTED.value},
                )
                self.session.flush()
                return ReviewResult(
                    version_id=version.id, status=ValuationStatus.REJECTED
                )
        except PublishingServiceError:
            raise
        except SQLAlchemyError as exc:
            raise PublishingServiceError("rejection_persistence_failed") from exc

    def publish_version(
        self,
        version_id: int,
        *,
        actor_user_id: int | None,
        reason: str | None = None,
        confirm_warnings: bool = False,
        actor_label: str | None = None,
        schedule_analysis: bool = True,
        ignore_validations: bool = False,
    ) -> PublicationResult:
        """Publish a validated version and supersede the current released version.

        ``ignore_validations`` is reserved for an explicit human publication
        decision. It records the waiver on each finding and in the audit log so
        the exception is not raised again by quality or review views.
        """

        try:
            with self.session.begin_nested():
                version = self._load_version(version_id, for_update=True)
                self._require_status(version, ValuationStatus.PUBLISHABLE)
                self._lock_fund(version.fund_id)
                self._ensure_publish_validation(
                    version,
                    confirm_warnings=confirm_warnings,
                    ignore_validations=ignore_validations,
                )
                superseded = self._supersede_current(
                    version, actor_user_id=actor_user_id
                )
                with self._allow_released_version_mutation(version):
                    version.status = ValuationStatus.PUBLISHED
                    version.published_at = utcnow()
                    version.published_by = actor_label or _actor_reference(
                        actor_user_id
                    )
                    version.release_reason = (
                        _optional_reason(reason)
                        or version.release_reason
                        or ("人工发布并忽略校验异常" if ignore_validations else None)
                    )
                    ignored_count = (
                        self._ignore_validation_findings(
                            version,
                            actor_user_id=actor_user_id,
                            reason=version.release_reason,
                        )
                        if ignore_validations
                        else 0
                    )
                    self.session.flush()
                analysis_run = (
                    self._create_analysis_run(version, "valuation_published")
                    if schedule_analysis
                    else None
                )
                self._audit(
                    action="valuation.published",
                    version=version,
                    actor_user_id=actor_user_id,
                    reason=version.release_reason,
                    summary={
                        "superseded_version_ids": [item.id for item in superseded],
                        "analysis_run_id": analysis_run.id if analysis_run else None,
                        "analysis_scheduled": analysis_run is not None,
                        "validation_ignored_count": ignored_count,
                    },
                )
                self.session.flush()
                return PublicationResult(
                    version_id=version.id,
                    fund_id=version.fund_id,
                    valuation_date=version.valuation_date,
                    superseded_version_ids=tuple(item.id for item in superseded),
                    analysis_run_id=analysis_run.id if analysis_run else None,
                    validation_ignored_count=ignored_count,
                )
        except PublishingServiceError:
            raise
        except IntegrityError as exc:
            raise PublishingConflictError("publication_conflict") from exc
        except SQLAlchemyError as exc:
            raise PublishingServiceError("publication_persistence_failed") from exc

    def revoke_version(
        self,
        version_id: int,
        *,
        actor_user_id: int | None,
        reason: str,
    ) -> PublicationResult:
        """Remove the current version from dashboards without deleting history."""

        revoke_reason = _required_reason(reason, "revoke_reason_required")
        try:
            with self.session.begin_nested():
                version = self._load_version(version_id, for_update=True)
                self._require_status(version, ValuationStatus.PUBLISHED)
                self._lock_fund(version.fund_id)
                with self._allow_released_version_mutation(version):
                    version.status = ValuationStatus.REVOKED
                    self.session.flush()
                analysis_run = self._create_analysis_run(version, "valuation_revoked")
                self._audit(
                    action="valuation.revoked",
                    version=version,
                    actor_user_id=actor_user_id,
                    reason=revoke_reason,
                    summary={"analysis_run_id": analysis_run.id},
                )
                self.session.flush()
                return PublicationResult(
                    version_id=version.id,
                    fund_id=version.fund_id,
                    valuation_date=version.valuation_date,
                    superseded_version_ids=(),
                    analysis_run_id=analysis_run.id,
                )
        except PublishingServiceError:
            raise
        except SQLAlchemyError as exc:
            raise PublishingServiceError("revoke_persistence_failed") from exc

    def restore_version(
        self,
        version_id: int,
        *,
        actor_user_id: int | None,
        reason: str,
        actor_label: str | None = None,
    ) -> PublicationResult:
        """Restore a superseded version as current and record a new release action."""

        restore_reason = _required_reason(reason, "restore_reason_required")
        try:
            with self.session.begin_nested():
                version = self._load_version(version_id, for_update=True)
                if ValuationStatus(version.status) not in {
                    ValuationStatus.SUPERSEDED,
                    ValuationStatus.REVOKED,
                }:
                    raise PublishingStateError("version_cannot_be_restored")
                self._lock_fund(version.fund_id)
                superseded = self._supersede_current(
                    version, actor_user_id=actor_user_id
                )
                with self._allow_released_version_mutation(version):
                    version.status = ValuationStatus.PUBLISHED
                    version.published_at = utcnow()
                    version.published_by = actor_label or _actor_reference(
                        actor_user_id
                    )
                    version.release_reason = restore_reason
                    self.session.flush()
                analysis_run = self._create_analysis_run(version, "valuation_restored")
                self._audit(
                    action="valuation.restored",
                    version=version,
                    actor_user_id=actor_user_id,
                    reason=restore_reason,
                    summary={
                        "superseded_version_ids": [item.id for item in superseded],
                        "analysis_run_id": analysis_run.id,
                    },
                )
                self._audit(
                    action="valuation.published",
                    version=version,
                    actor_user_id=actor_user_id,
                    reason=restore_reason,
                    summary={
                        "publication_kind": "restore",
                        "analysis_run_id": analysis_run.id,
                    },
                )
                self.session.flush()
                return PublicationResult(
                    version_id=version.id,
                    fund_id=version.fund_id,
                    valuation_date=version.valuation_date,
                    superseded_version_ids=tuple(item.id for item in superseded),
                    analysis_run_id=analysis_run.id,
                )
        except PublishingServiceError:
            raise
        except IntegrityError as exc:
            raise PublishingConflictError("publication_conflict") from exc
        except SQLAlchemyError as exc:
            raise PublishingServiceError("restore_persistence_failed") from exc

    def _load_version(
        self, version_id: int, *, for_update: bool = False
    ) -> ValuationVersion:
        statement = select(ValuationVersion).where(ValuationVersion.id == version_id)
        if for_update and self._supports_row_locks:
            statement = statement.with_for_update()
        version = self.session.scalar(statement)
        if version is None:
            raise PublishingStateError("valuation_version_not_found")
        return version

    def _lock_fund(self, fund_id: int) -> None:
        statement = select(Fund.id).where(Fund.id == fund_id)
        if self._supports_row_locks:
            statement = statement.with_for_update()
        if self.session.scalar(statement) is None:
            raise PublishingStateError("fund_not_found")

    def _supersede_current(
        self, target: ValuationVersion, *, actor_user_id: int | None
    ) -> tuple[ValuationVersion, ...]:
        statement = (
            select(ValuationVersion)
            .where(
                ValuationVersion.fund_id == target.fund_id,
                ValuationVersion.valuation_date == target.valuation_date,
                ValuationVersion.status == ValuationStatus.PUBLISHED,
                ValuationVersion.id != target.id,
            )
            .order_by(ValuationVersion.id)
        )
        if self._supports_row_locks:
            statement = statement.with_for_update()
        current = tuple(self.session.scalars(statement).all())
        with self._allow_released_version_mutation(*current):
            for version in current:
                version.status = ValuationStatus.SUPERSEDED
                self._audit(
                    action="valuation.superseded",
                    version=version,
                    actor_user_id=actor_user_id,
                    summary={"replacement_version_id": target.id},
                )
            if current:
                self.session.flush()
        return current

    @contextmanager
    def _allow_released_version_mutation(
        self, *versions: ValuationVersion
    ) -> Iterator[None]:
        previous = set(self.session.info.get(RELEASED_VERSION_MUTATION_KEY, set()))
        self.session.info[RELEASED_VERSION_MUTATION_KEY] = {
            *previous,
            *(version.id for version in versions),
        }
        try:
            yield
        finally:
            if previous:
                self.session.info[RELEASED_VERSION_MUTATION_KEY] = previous
            else:
                self.session.info.pop(RELEASED_VERSION_MUTATION_KEY, None)

    def _ensure_publish_validation(
        self,
        version: ValuationVersion,
        *,
        confirm_warnings: bool,
        ignore_validations: bool,
    ) -> None:
        levels = tuple(
            self.session.scalars(
                select(ValidationResult.level).where(
                    ValidationResult.valuation_version_id == version.id
                )
            ).all()
        )
        if not levels:
            raise PublishingValidationError("validation_required")
        if ignore_validations:
            return
        if ValidationLevel.WARNING in levels and not confirm_warnings:
            raise PublishingValidationError("warning_confirmation_required")
        if ValidationLevel.CRITICAL in levels and not self._review_was_approved(
            version.id
        ):
            raise PublishingValidationError("critical_validation_unapproved")

    def _ignore_validation_findings(
        self,
        version: ValuationVersion,
        *,
        actor_user_id: int | None,
        reason: str | None,
    ) -> int:
        findings = list(
            self.session.scalars(
                select(ValidationResult).where(
                    ValidationResult.valuation_version_id == version.id,
                    ValidationResult.ignored.is_(False),
                )
            ).all()
        )
        if not findings:
            return 0
        # The publication transaction temporarily authorizes this version's
        # finding rows so the immutable-detail guard does not reject the
        # intentional waiver metadata update.
        allowed = set(self.session.info.get(RELEASED_VERSION_MUTATION_KEY, set()))
        self.session.info[RELEASED_VERSION_MUTATION_KEY] = {*allowed, version.id}
        ignored_at = utcnow()
        for finding in findings:
            finding.ignored = True
            finding.ignored_at = ignored_at
            finding.ignored_by_user_id = actor_user_id
            finding.ignored_reason = reason
        self._audit(
            action="valuation.validation_ignored",
            version=version,
            actor_user_id=actor_user_id,
            reason=reason,
            summary={
                "finding_count": len(findings),
                "rule_codes": [finding.rule_code for finding in findings],
            },
        )
        return len(findings)

    def _review_was_approved(self, version_id: int) -> bool:
        return (
            self.session.scalar(
                select(AuditLog.id)
                .where(
                    AuditLog.resource_type == "valuation_version",
                    AuditLog.resource_id == str(version_id),
                    AuditLog.action == "valuation.review_approved",
                    AuditLog.result == AuditResult.SUCCESS,
                )
                .limit(1)
            )
            is not None
        )

    def _create_analysis_run(
        self,
        version: ValuationVersion,
        trigger_reason: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AnalysisRun:
        end_label = end_date.isoformat() if end_date is not None else "latest"
        input_version_range = (
            f"fund:{version.fund_id};dates:{start_date.isoformat()}..{end_label}"
            if start_date is not None
            else f"valuation_version:{version.id}"
        )
        analysis_run = AnalysisRun(
            trigger_version_id=version.id,
            trigger_reason=trigger_reason,
            input_start_date=start_date or version.valuation_date,
            input_end_date=end_date,
            input_version_range=input_version_range,
            methodology_version=self.methodology_version,
            status=AnalysisRunStatus.QUEUED,
        )
        self.session.add(analysis_run)
        self.session.flush()
        self.session.add(
            BackgroundJob(
                job_type="process_analysis_run",
                resource_id=str(analysis_run.id),
            )
        )
        self.session.flush()
        return analysis_run

    def queue_analysis_run(
        self,
        version_id: int,
        *,
        start_date: date,
        end_date: date | None = None,
        actor_user_id: int | None,
        trigger_reason: str,
    ) -> AnalysisRun:
        """Queue analysis through the latest data unless an end is requested."""

        if end_date is not None and start_date > end_date:
            raise PublishingStateError("analysis_date_range_invalid")
        try:
            with self.session.begin_nested():
                version = self._load_version(version_id, for_update=True)
                self._require_status(version, ValuationStatus.PUBLISHED)
                run = self._create_analysis_run(
                    version,
                    trigger_reason,
                    start_date=start_date,
                    end_date=end_date,
                )
                self.session.add(
                    AuditLog(
                        actor_user_id=actor_user_id,
                        action="analysis.queued",
                        resource_type="analysis_run",
                        resource_id=str(run.id),
                        summary={
                            "trigger_version_id": version.id,
                            "fund_id": version.fund_id,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat() if end_date else None,
                        },
                        result=AuditResult.SUCCESS,
                    )
                )
                self.session.flush()
                return run
        except PublishingServiceError:
            raise
        except SQLAlchemyError as exc:
            raise PublishingServiceError("analysis_queue_persistence_failed") from exc

    def _audit(
        self,
        *,
        action: str,
        version: ValuationVersion,
        actor_user_id: int | None,
        reason: str | None = None,
        summary: dict[str, object] | None = None,
    ) -> None:
        payload = {
            "fund_id": version.fund_id,
            "valuation_date": version.valuation_date.isoformat(),
            "version_no": version.version_no,
            **(summary or {}),
        }
        self.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                resource_type="valuation_version",
                resource_id=str(version.id),
                summary=payload,
                reason=reason,
                result=AuditResult.SUCCESS,
            )
        )

    @staticmethod
    def _require_status(version: ValuationVersion, required: ValuationStatus) -> None:
        if ValuationStatus(version.status) != required:
            raise PublishingStateError(
                f"invalid_status_for_action:{ValuationStatus(version.status).value}"
            )

    @property
    def _supports_row_locks(self) -> bool:
        return (
            self.session.bind is not None
            and self.session.bind.dialect.name == "postgresql"
        )


def _coerce_status(value: object) -> ValuationStatus | None:
    try:
        return ValuationStatus(value)
    except (TypeError, ValueError):
        return None


def _required_reason(value: str, error_code: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise PublishingStateError(error_code)
    return normalized


def _optional_reason(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _actor_reference(actor_user_id: int | None) -> str | None:
    return f"user:{actor_user_id}" if actor_user_id is not None else None


__all__ = [
    "PublicationResult",
    "PublishedVersionImmutableError",
    "PublishingConflictError",
    "PublishingService",
    "PublishingServiceError",
    "PublishingStateError",
    "PublishingValidationError",
    "ReviewResult",
]
