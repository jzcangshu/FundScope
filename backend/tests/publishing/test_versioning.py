from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db.base import (
    AnalysisRunStatus,
    AuditResult,
    UserRole,
    UserStatus,
    ValidationLevel,
    ValuationStatus,
)
from app.db.models import (
    AnalysisRun,
    AuditLog,
    Fund,
    FundDailySnapshot,
    User,
    ValidationResult,
    ValuationVersion,
)
from app.publishing import (
    PublishedVersionImmutableError,
    PublishingService,
    PublishingStateError,
    PublishingValidationError,
)

VALUATION_DATE = date(2026, 8, 25)


def _actor(session: Session) -> User:
    actor = User(
        username="operator",
        password_hash="not-used-by-workflow-tests",
        role=UserRole.OPERATOR,
        status=UserStatus.ACTIVE,
    )
    session.add(actor)
    session.flush()
    return actor


def _fund(session: Session) -> Fund:
    fund = Fund(standard_name="发布测试产品")
    session.add(fund)
    session.flush()
    return fund


def _version(
    session: Session,
    fund: Fund,
    version_no: int,
    *,
    status: ValuationStatus = ValuationStatus.PUBLISHABLE,
    level: ValidationLevel = ValidationLevel.INFO,
) -> ValuationVersion:
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=VALUATION_DATE,
        version_no=version_no,
        status=status,
    )
    session.add(version)
    session.flush()
    session.add_all(
        [
            FundDailySnapshot(
                valuation_version_id=version.id,
                total_assets=Decimal(100),
                total_liabilities=Decimal(30),
                net_asset_value=Decimal(70),
            ),
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="test_rule",
                level=level,
                message="测试校验结果",
            ),
        ]
    )
    session.flush()
    return version


def _released_version(session: Session, status: ValuationStatus) -> ValuationVersion:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1)
    session.commit()
    service = PublishingService(session)
    service.publish_version(version.id, actor_user_id=actor.id)
    session.commit()
    if status == ValuationStatus.SUPERSEDED:
        replacement = _version(session, fund, 2)
        session.commit()
        service.publish_version(replacement.id, actor_user_id=actor.id)
        session.commit()
    elif status == ValuationStatus.REVOKED:
        service.revoke_version(version.id, actor_user_id=actor.id, reason="撤回测试")
        session.commit()
    return version


def _mutate_version_parent(
    session: Session, version: ValuationVersion, field: str
) -> None:
    if field == "fund_id":
        other_fund = Fund(standard_name="发布测试产品-其他")
        session.add(other_fund)
        session.flush()
        version.fund_id = other_fund.id
    elif field == "valuation_date":
        version.valuation_date = date(2026, 8, 26)
    elif field == "version_no":
        version.version_no = 99
    elif field == "source_file_id":
        version.source_file_id = 999_999
    elif field == "parser_rule_set_id":
        version.parser_rule_set_id = 999_999
    elif field == "status":
        version.status = ValuationStatus.PUBLISHABLE
    elif field == "published_by":
        version.published_by = "tampered-actor"
    elif field == "release_reason":
        version.release_reason = "篡改发布原因"
    elif field == "published_at":
        version.published_at = datetime(2030, 1, 1, tzinfo=UTC)
    else:
        raise AssertionError(f"unknown field: {field}")


def test_publish_creates_audit_and_analysis_run(session: Session) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1)
    session.commit()

    result = PublishingService(session).publish_version(
        version.id,
        actor_user_id=actor.id,
        actor_label=actor.username,
        reason="首次发布",
    )
    session.commit()

    released = session.get(ValuationVersion, version.id)
    analysis_run = session.get(AnalysisRun, result.analysis_run_id)
    audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "valuation.published")
    )
    assert released.status == ValuationStatus.PUBLISHED
    assert released.published_by == "operator"
    assert released.release_reason == "首次发布"
    assert released.published_at is not None
    assert analysis_run.status == AnalysisRunStatus.QUEUED
    assert analysis_run.input_start_date == VALUATION_DATE
    assert audit.actor_user_id == actor.id
    assert audit.result == AuditResult.SUCCESS


def test_new_publication_supersedes_old_version_and_leaves_only_one_current(
    session: Session,
) -> None:
    actor = _actor(session)
    fund = _fund(session)
    first = _version(session, fund, 1)
    second = _version(session, fund, 2)
    session.commit()
    service = PublishingService(session)

    service.publish_version(first.id, actor_user_id=actor.id)
    session.commit()
    result = service.publish_version(second.id, actor_user_id=actor.id)
    session.commit()

    published_count = session.scalar(
        select(func.count())
        .select_from(ValuationVersion)
        .where(
            ValuationVersion.fund_id == fund.id,
            ValuationVersion.valuation_date == VALUATION_DATE,
            ValuationVersion.status == ValuationStatus.PUBLISHED,
        )
    )
    assert result.superseded_version_ids == (first.id,)
    assert session.get(ValuationVersion, first.id).status == ValuationStatus.SUPERSEDED
    assert session.get(ValuationVersion, second.id).status == ValuationStatus.PUBLISHED
    assert published_count == 1
    supersede_audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "valuation.superseded")
    )
    assert supersede_audit.actor_user_id == actor.id
    assert supersede_audit.summary["replacement_version_id"] == second.id


def test_warning_requires_explicit_confirmation(session: Session) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1, level=ValidationLevel.WARNING)
    session.commit()
    service = PublishingService(session)

    with pytest.raises(
        PublishingValidationError, match="warning_confirmation_required"
    ):
        service.publish_version(version.id, actor_user_id=actor.id)
    assert version.status == ValuationStatus.PUBLISHABLE

    service.publish_version(
        version.id,
        actor_user_id=actor.id,
        confirm_warnings=True,
    )
    session.commit()
    assert version.status == ValuationStatus.PUBLISHED


def test_critical_result_requires_review_before_publication(session: Session) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(
        session,
        fund,
        1,
        status=ValuationStatus.PENDING_REVIEW,
        level=ValidationLevel.CRITICAL,
    )
    session.commit()
    service = PublishingService(session)

    pending = service.pending_reviews(fund_id=fund.id)
    assert [item.id for item in pending] == [version.id]

    review = service.complete_review(
        version.id,
        approved=True,
        actor_user_id=actor.id,
        note="已核对原始估值表，确认可发布",
    )
    assert review.status == ValuationStatus.PUBLISHABLE
    service.publish_version(version.id, actor_user_id=actor.id)
    session.commit()

    assert version.status == ValuationStatus.PUBLISHED
    actions = set(
        session.scalars(
            select(AuditLog.action).where(AuditLog.resource_id == str(version.id))
        ).all()
    )
    assert {"valuation.review_approved", "valuation.published"} <= actions


def test_review_rejection_is_terminal_and_audited(session: Session) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(
        session,
        fund,
        1,
        status=ValuationStatus.PENDING_REVIEW,
        level=ValidationLevel.CRITICAL,
    )
    session.commit()

    result = PublishingService(session).complete_review(
        version.id,
        approved=False,
        actor_user_id=actor.id,
        note="产品身份不匹配",
    )
    session.commit()

    assert result.status == ValuationStatus.REJECTED
    with pytest.raises(
        PublishingStateError, match="invalid_status_for_action:rejected"
    ):
        PublishingService(session).publish_version(version.id, actor_user_id=actor.id)
    audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "valuation.review_rejected")
    )
    assert audit.reason == "产品身份不匹配"


def test_revoke_removes_current_version_and_is_audited(session: Session) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1)
    session.commit()
    service = PublishingService(session)
    service.publish_version(version.id, actor_user_id=actor.id)
    session.commit()

    result = service.revoke_version(
        version.id,
        actor_user_id=actor.id,
        reason="发现来源文件有误",
    )
    session.commit()

    assert version.status == ValuationStatus.REVOKED
    assert (
        session.get(AnalysisRun, result.analysis_run_id).status
        == AnalysisRunStatus.QUEUED
    )
    audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "valuation.revoked")
    )
    assert audit.reason == "发现来源文件有误"


def test_restore_old_version_supersedes_current_and_records_new_release_action(
    session: Session,
) -> None:
    actor = _actor(session)
    fund = _fund(session)
    first = _version(session, fund, 1)
    second = _version(session, fund, 2)
    session.commit()
    service = PublishingService(session)
    service.publish_version(first.id, actor_user_id=actor.id)
    session.commit()
    service.publish_version(second.id, actor_user_id=actor.id)
    session.commit()

    result = service.restore_version(
        first.id,
        actor_user_id=actor.id,
        actor_label=actor.username,
        reason="回退到已复核的上一版",
    )
    session.commit()

    assert result.superseded_version_ids == (second.id,)
    assert first.status == ValuationStatus.PUBLISHED
    assert second.status == ValuationStatus.SUPERSEDED
    assert first.release_reason == "回退到已复核的上一版"
    restore_audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "valuation.restored")
    )
    assert restore_audit.reason == "回退到已复核的上一版"
    assert restore_audit.summary["superseded_version_ids"] == [second.id]
    restore_publish_audit = session.scalar(
        select(AuditLog).where(
            AuditLog.action == "valuation.published",
            AuditLog.resource_id == str(first.id),
            AuditLog.summary["publication_kind"].as_string() == "restore",
        )
    )
    assert restore_publish_audit is not None


def test_revoked_version_can_be_restored_when_no_newer_version_is_current(
    session: Session,
) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1)
    session.commit()
    service = PublishingService(session)
    service.publish_version(version.id, actor_user_id=actor.id)
    session.commit()
    service.revoke_version(version.id, actor_user_id=actor.id, reason="撤回校验")
    session.commit()

    service.restore_version(version.id, actor_user_id=actor.id, reason="恢复已确认版本")
    session.commit()

    assert version.status == ValuationStatus.PUBLISHED


def test_released_version_details_cannot_be_updated_or_deleted(
    session: Session,
) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1)
    session.commit()
    service = PublishingService(session)
    service.publish_version(version.id, actor_user_id=actor.id)
    session.commit()
    snapshot = session.scalar(
        select(FundDailySnapshot).where(
            FundDailySnapshot.valuation_version_id == version.id
        )
    )

    snapshot.net_asset_value = Decimal(71)
    with pytest.raises(
        PublishedVersionImmutableError,
        match="published_version_details_are_immutable",
    ):
        session.flush()
    session.rollback()

    snapshot = session.scalar(
        select(FundDailySnapshot).where(
            FundDailySnapshot.valuation_version_id == version.id
        )
    )
    session.delete(snapshot)
    with pytest.raises(PublishedVersionImmutableError):
        session.flush()


def test_detail_guard_loads_candidate_versions_in_one_query(session: Session) -> None:
    fund = _fund(session)
    first = _version(session, fund, 1)
    second = _version(session, fund, 2)
    first_id = first.id
    second_id = second.id
    session.commit()
    session.expunge_all()
    snapshots = session.scalars(
        select(FundDailySnapshot).where(
            FundDailySnapshot.valuation_version_id.in_((first_id, second_id))
        )
    ).all()
    for snapshot in snapshots:
        snapshot.net_asset_value = Decimal(72)

    version_selects = 0

    def count_version_selects(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal version_selects
        normalized = statement.lower()
        if (
            normalized.lstrip().startswith("select")
            and "from valuation_version" in normalized
        ):
            version_selects += 1

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count_version_selects)
    try:
        session.flush()
    finally:
        event.remove(engine, "before_cursor_execute", count_version_selects)

    assert version_selects == 1


def test_released_version_parent_cannot_be_deleted(session: Session) -> None:
    actor = _actor(session)
    fund = _fund(session)
    version = _version(session, fund, 1)
    session.commit()
    PublishingService(session).publish_version(version.id, actor_user_id=actor.id)
    session.commit()

    session.delete(version)
    with pytest.raises(
        PublishedVersionImmutableError,
        match="published_version_parent_is_immutable",
    ):
        session.flush()


@pytest.mark.parametrize(
    "status",
    [
        ValuationStatus.PUBLISHED,
        ValuationStatus.SUPERSEDED,
        ValuationStatus.REVOKED,
    ],
)
@pytest.mark.parametrize(
    "field",
    [
        "fund_id",
        "valuation_date",
        "version_no",
        "source_file_id",
        "parser_rule_set_id",
        "status",
        "published_by",
        "release_reason",
        "published_at",
    ],
)
def test_released_version_parent_fields_cannot_be_modified_directly(
    session: Session,
    status: ValuationStatus,
    field: str,
) -> None:
    version = _released_version(session, status)
    assert version.status == status

    _mutate_version_parent(session, version, field)

    with pytest.raises(
        PublishedVersionImmutableError,
        match="published_version_parent_is_immutable",
    ):
        session.flush()
