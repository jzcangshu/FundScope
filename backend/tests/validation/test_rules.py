from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.db.base import ValidationLevel, ValuationStatus
from app.parser.interface import ParsedPosition, ParsedShareClass, ParsedValuation
from app.validation.rules import (
    check_asset_liability_balance,
    check_daily_return,
    check_position_market_value,
    check_share_net_assets_total,
    validate_parsed_valuation,
    validate_values,
)


def test_asset_liability_balance_reports_info_and_critical() -> None:
    passed = check_asset_liability_balance(Decimal(100), Decimal(30), Decimal(70))
    failed = check_asset_liability_balance(Decimal(100), Decimal(30), Decimal(69))

    assert passed.level == ValidationLevel.INFO
    assert passed.difference == Decimal(0)
    assert failed.level == ValidationLevel.CRITICAL
    assert failed.difference == Decimal(1)


def test_share_assets_and_daily_return_rules_are_decimal_safe() -> None:
    shares = (
        ParsedShareClass(
            share_code="A",
            share_name="A",
            net_assets=Decimal(40),
            paid_in_capital=None,
            unit_nav=None,
            cumulative_unit_nav=None,
            previous_unit_nav=None,
            daily_return=None,
        ),
        ParsedShareClass(
            share_code="B",
            share_name="B",
            net_assets=Decimal(60),
            paid_in_capital=None,
            unit_nav=None,
            cumulative_unit_nav=None,
            previous_unit_nav=None,
            daily_return=None,
        ),
    )
    share_result = check_share_net_assets_total(shares, Decimal(100))
    return_result = check_daily_return(Decimal("1.02"), Decimal(1), Decimal("0.01"))

    assert share_result.level == ValidationLevel.INFO
    assert return_result.level == ValidationLevel.WARNING
    assert return_result.actual_value == Decimal("0.02")


def test_position_quantity_times_price_is_warning_when_different() -> None:
    position = ParsedPosition(
        security_code="000001",
        security_name="测试证券",
        quantity=Decimal(10),
        unit_cost=None,
        cost=None,
        market_price=Decimal(3),
        market_value=Decimal(29),
        nav_weight=None,
        valuation_gain=None,
        suspension_info=None,
        source_subject_code="000001",
        market=None,
        account=None,
        source_row=1,
    )

    result = check_position_market_value(position)

    assert result.level == ValidationLevel.WARNING
    assert result.actual_value == Decimal(30)
    assert result.difference == Decimal(1)


def test_position_relative_tolerance_allows_large_drift() -> None:
    # A 100万元 position with 0.005 yuan total drift is well within 0.01%
    # relative tolerance — should pass, not flag as WARNING.
    position = ParsedPosition(
        security_code="600000",
        security_name="大额持仓",
        quantity=Decimal(100000),
        unit_cost=None,
        cost=None,
        market_price=Decimal("10.00"),
        market_value=Decimal("1000000.005"),
        nav_weight=None,
        valuation_gain=None,
        suspension_info=None,
        source_subject_code="600000",
        market=None,
        account=None,
        source_row=1,
    )

    result = check_position_market_value(position)

    assert result.level == ValidationLevel.INFO


def test_position_relative_tolerance_still_catches_real_drift() -> None:
    # A 1万元 position with 100元 drift (1%) is well above the relative
    # tolerance and must still warn.
    position = ParsedPosition(
        security_code="600001",
        security_name="中等持仓",
        quantity=Decimal(100),
        unit_cost=None,
        cost=None,
        market_price=Decimal(100),
        market_value=Decimal(10100),
        nav_weight=None,
        valuation_gain=None,
        suspension_info=None,
        source_subject_code="600001",
        market=None,
        account=None,
        source_row=1,
    )

    result = check_position_market_value(position)

    assert result.level == ValidationLevel.WARNING


def test_validate_values_is_publishable_with_warnings_but_not_critical() -> None:
    warning_report = validate_values(
        total_assets=Decimal(100),
        total_liabilities=Decimal(30),
        net_asset_value=Decimal(70),
        unit_nav=Decimal("1.02"),
        previous_unit_nav=Decimal(1),
        daily_return=Decimal("0.01"),
    )
    critical_report = validate_values(
        total_assets=Decimal(100),
        total_liabilities=Decimal(30),
        net_asset_value=Decimal(69),
        unit_nav=None,
        previous_unit_nav=None,
        daily_return=None,
    )

    assert warning_report.status == ValuationStatus.PUBLISHABLE
    assert warning_report.warning_count == 1
    assert critical_report.status == ValuationStatus.PENDING_REVIEW
    assert critical_report.critical_count >= 1


def test_parsed_valuation_identity_and_parser_warnings_are_included() -> None:
    parsed = ParsedValuation(
        product_name=None,
        product_candidates=(),
        valuation_date=None,
        worksheet="估值表",
        total_assets=None,
        total_liabilities=None,
        net_asset_value=None,
        unit_nav=None,
        cumulative_unit_nav=None,
        previous_unit_nav=None,
        daily_return=None,
        ytd_return=None,
        mtd_return=None,
        qtd_return=None,
        wtd_return=None,
        cumulative_return=None,
        cumulative_payout=None,
        available_headroom=None,
        warnings=("valuation_date_unrecognized",),
    )

    report = validate_parsed_valuation(parsed)

    assert report.status == ValuationStatus.PENDING_REVIEW
    assert {finding.level for finding in report.findings} == {
        ValidationLevel.CRITICAL,
        ValidationLevel.INFO,
    }


def test_validation_service_persists_results(
    session: Session,
) -> None:
    from app.db.models import (
        Fund,
        FundDailySnapshot,
        ValidationResult,
        ValuationVersion,
    )
    from app.validation.service import ValidationService

    fund = Fund(standard_name="校验产品")
    session.add(fund)
    session.flush()
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=date(2026, 8, 25),
        version_no=1,
        status=ValuationStatus.RECEIVED,
    )
    session.add(version)
    session.flush()
    session.add(
        FundDailySnapshot(
            valuation_version_id=version.id,
            total_assets=Decimal(100),
            total_liabilities=Decimal(30),
            net_asset_value=Decimal(69),
        )
    )
    session.commit()

    report = ValidationService(session).validate_version(version.id)
    session.commit()

    assert report.status == ValuationStatus.PENDING_REVIEW
    assert (
        session.get(ValuationVersion, version.id).status
        == ValuationStatus.PENDING_REVIEW
    )
    result_count = (
        session.query(ValidationResult)
        .filter_by(valuation_version_id=version.id)
        .count()
    )
    assert result_count >= 3


@pytest.mark.parametrize(
    ("status", "error_code"),
    [
        (ValuationStatus.FAILED, "invalid_status_for_validation:failed"),
        (ValuationStatus.DUPLICATE, "invalid_status_for_validation:duplicate"),
        (
            ValuationStatus.NON_VALUATION,
            "invalid_status_for_validation:non_valuation",
        ),
        (ValuationStatus.REJECTED, "rejected_version_is_immutable"),
        (ValuationStatus.PUBLISHED, "published_version_is_immutable"),
        (ValuationStatus.SUPERSEDED, "published_version_is_immutable"),
        (ValuationStatus.REVOKED, "published_version_is_immutable"),
    ],
)
def test_validation_service_rejects_terminal_lifecycle_states(
    session: Session,
    status: ValuationStatus,
    error_code: str,
) -> None:
    from app.db.models import Fund, ValuationVersion
    from app.validation.service import ValidationService, ValidationStateError

    fund = Fund(standard_name=f"终态校验产品-{status.value}")
    session.add(fund)
    session.flush()
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=date(2026, 8, 25),
        version_no=1,
        status=status,
    )
    session.add(version)
    session.commit()

    with pytest.raises(ValidationStateError, match=error_code):
        ValidationService(session).validate_version(version.id)


def test_validation_service_accepts_parser_import_state(session: Session) -> None:
    from app.db.models import Fund, ValuationVersion
    from app.validation.service import ValidationService

    fund = Fund(standard_name="解析状态兼容产品")
    session.add(fund)
    session.flush()
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=date(2026, 8, 25),
        version_no=1,
        status=ValuationStatus.PARSING,
    )
    session.add(version)
    session.commit()

    report = ValidationService(session).validate_version(version.id)

    # Missing snapshot is blocking: publishing it would make analytics omit
    # the valuation through its inner join.
    assert report.status == ValuationStatus.PENDING_REVIEW
    finding = next(
        f for f in report.findings if f.rule_code == "valuation_snapshot_missing"
    )
    assert finding.level == ValidationLevel.CRITICAL


def test_postgresql_validation_load_locks_version_row() -> None:
    from app.validation.service import ValidationService, ValidationVersionNotFound

    class RecordingSession:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        statement: object | None = None

        def scalar(self, statement: object) -> None:
            self.statement = statement

    recording_session = RecordingSession()
    service = ValidationService(cast(Session, recording_session))

    with pytest.raises(ValidationVersionNotFound):
        service._load_version(123)

    assert recording_session.statement is not None
    assert recording_session.statement._for_update_arg is not None  # type: ignore[attr-defined]
