from datetime import date
from decimal import Decimal

from app.parser.normalizers import decimal, parse_date, ratio
from app.validation.rules import validate_values


def test_normalize_amount_and_percentage_text() -> None:
    assert decimal("10,824,713.18") == Decimal("10824713.18")
    assert decimal("-") is None
    assert ratio("2.0481") == Decimal("0.020481")


def test_parse_supported_valuation_dates() -> None:
    assert parse_date("估值日期：2026-03-06") == date(2026, 3, 6)
    assert parse_date("估值日期：20260402") == date(2026, 4, 2)
    assert parse_date("估值日期：2026年5月9日") == date(2026, 5, 9)


def test_decimal_rejects_non_finite_values() -> None:
    for value in (
        "NaN",
        "Infinity",
        "-Infinity",
        Decimal("NaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
    ):
        assert decimal(value) is None


def test_validation_receives_missing_value_instead_of_non_finite_decimal() -> None:
    report = validate_values(
        total_assets=decimal("NaN"),
        total_liabilities=Decimal(10),
        net_asset_value=Decimal(10),
        unit_nav=None,
        previous_unit_nav=None,
        daily_return=None,
    )

    assert report.critical_count >= 1


def test_decimal_disambiguates_us_vs_eu_locale() -> None:
    # US: comma is thousands separator, period is decimal — last sep is '.'.
    assert decimal("10,824,713.18") == Decimal("10824713.18")
    assert decimal("1,234.56") == Decimal("1234.56")
    # EU: period is thousands separator, comma is decimal — last sep is ','.
    assert decimal("1.234,56") == Decimal("1234.56")
    assert decimal("10.824.713,18") == Decimal("10824713.18")
    # Chinese full-width comma is treated as ASCII comma.
    assert decimal("1，234.56") == Decimal("1234.56")
