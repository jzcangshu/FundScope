"""Ordered removal of a fund and every row that depends on it.

Product data foreign keys use ``RESTRICT``: the database refuses to drop a parent
row while a child still references it. A fund therefore has to be dismantled
from the deepest dependent table upwards, and that order must live in exactly
one place -- this module. Account grants are removed alongside product data.

Two deliberate exceptions, both about preserving history that was never
exclusively about the fund being removed:

* ``analysis_run.trigger_version_id`` is nullable, so runs triggered by the
  fund's valuations are *detached* (set to NULL) instead of deleted.
  ``company_metric_daily.source_analysis_run_id`` is NOT nullable, so deleting
  those runs would have taken the company-level metric history down with them --
  history that also covers every other fund that was active on those dates.
* Source files are preserved. They are shared mail attachments governed by the
  retention job, not per-fund property, and several funds can share one. Their
  nullable recognized-product hint uses ``SET NULL`` when the product is removed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    AccountSubjectDaily,
    AnalysisRun,
    CompanyMetricDaily,
    FieldProvenance,
    Fund,
    FundAlias,
    FundDailySnapshot,
    FundMetricDaily,
    PositionDaily,
    RiskEvent,
    ShareClass,
    ShareClassDailySnapshot,
    UserFundAccess,
    ValidationResult,
    ValuationVersion,
)

# Rows that hang off a valuation version, listed deepest-first. The tuple order
# is the deletion order, so keep it in dependency order.
_VERSION_CHILDREN: tuple[tuple[Any, str], ...] = (
    (ShareClassDailySnapshot, "share_class_daily_snapshot"),
    (AccountSubjectDaily, "account_subject_daily"),
    (FieldProvenance, "field_provenance"),
    (FundDailySnapshot, "fund_daily_snapshot"),
    (PositionDaily, "position_daily"),
    (ValidationResult, "validation_result"),
)


def _child_condition(
    model: Any, *, version_ids: list[int], share_class_ids: list[int]
) -> ColumnElement[bool]:
    """Scope one version-child table to the fund being removed."""

    if model is ShareClassDailySnapshot:
        # Reachable two ways: through the share class, or through the version.
        return or_(
            model.valuation_version_id.in_(version_ids),
            model.share_class_id.in_(share_class_ids),
        )
    return model.valuation_version_id.in_(version_ids)


def _collect_ids(
    session: Session, fund_id: int
) -> tuple[list[int], list[int], list[int]]:
    """Return (version ids, share class ids, analysis run ids) for the fund."""

    version_ids = list(
        session.scalars(
            select(ValuationVersion.id).where(ValuationVersion.fund_id == fund_id)
        ).all()
    )
    share_class_ids = list(
        session.scalars(
            select(ShareClass.id).where(ShareClass.fund_id == fund_id)
        ).all()
    )
    run_ids = (
        list(
            session.scalars(
                select(AnalysisRun.id).where(
                    AnalysisRun.trigger_version_id.in_(version_ids)
                )
            ).all()
        )
        if version_ids
        else []
    )
    return version_ids, share_class_ids, run_ids


def _count(session: Session, model: Any, condition: ColumnElement[bool]) -> int:
    return int(
        session.scalar(select(func.count()).select_from(model).where(condition)) or 0
    )


def collect_fund_removal_impact(
    session: Session, fund_id: int
) -> dict[str, object] | None:
    """Describe what removing a fund would destroy, without changing anything."""

    if session.get(Fund, fund_id) is None:
        return None
    version_ids, share_class_ids, run_ids = _collect_ids(session, fund_id)

    removed: dict[str, int] = {}
    for model, name in _VERSION_CHILDREN:
        removed[name] = _count(
            session,
            model,
            _child_condition(
                model, version_ids=version_ids, share_class_ids=share_class_ids
            ),
        )
    removed["fund_metric_daily"] = _count(
        session, FundMetricDaily, FundMetricDaily.fund_id == fund_id
    )
    removed["valuation_version"] = _count(
        session, ValuationVersion, ValuationVersion.fund_id == fund_id
    )
    removed["share_class"] = _count(session, ShareClass, ShareClass.fund_id == fund_id)
    removed["risk_event"] = _count(session, RiskEvent, RiskEvent.fund_id == fund_id)
    removed["fund_alias"] = _count(session, FundAlias, FundAlias.fund_id == fund_id)
    removed["user_fund_access"] = _count(
        session, UserFundAccess, UserFundAccess.fund_id == fund_id
    )
    removed["fund"] = 1

    return {
        "removed": removed,
        "total_removed": sum(removed.values()),
        "detached_analysis_runs": _count(
            session, AnalysisRun, AnalysisRun.trigger_version_id.in_(version_ids)
        )
        if version_ids
        else 0,
        "preserved_company_metrics": _count(
            session,
            CompanyMetricDaily,
            CompanyMetricDaily.source_analysis_run_id.in_(run_ids),
        )
        if run_ids
        else 0,
    }


def remove_fund(session: Session, fund_id: int) -> dict[str, object] | None:
    """Delete a fund and every dependent row. Caller owns the transaction."""

    if session.get(Fund, fund_id) is None:
        return None
    impact = collect_fund_removal_impact(session, fund_id)
    version_ids, share_class_ids, _run_ids = _collect_ids(session, fund_id)
    if impact is None:  # pragma: no cover - guarded by the lookup above
        return None

    for model, _name in _VERSION_CHILDREN:
        session.execute(
            delete(model).where(
                _child_condition(
                    model, version_ids=version_ids, share_class_ids=share_class_ids
                )
            )
        )
    if version_ids:
        # Detach rather than delete: company_metric_daily.source_analysis_run_id
        # is NOT NULL, so removing the run would erase company-level history.
        session.execute(
            update(AnalysisRun)
            .where(AnalysisRun.trigger_version_id.in_(version_ids))
            .values(trigger_version_id=None)
        )
    session.execute(delete(FundMetricDaily).where(FundMetricDaily.fund_id == fund_id))
    session.execute(delete(ValuationVersion).where(ValuationVersion.fund_id == fund_id))
    session.execute(delete(ShareClass).where(ShareClass.fund_id == fund_id))
    session.execute(delete(RiskEvent).where(RiskEvent.fund_id == fund_id))
    session.execute(delete(FundAlias).where(FundAlias.fund_id == fund_id))
    session.execute(delete(UserFundAccess).where(UserFundAccess.fund_id == fund_id))
    session.execute(delete(Fund).where(Fund.id == fund_id))
    session.flush()
    return impact
