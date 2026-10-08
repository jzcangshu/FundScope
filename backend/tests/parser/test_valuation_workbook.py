from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.db.base import ValidationLevel
from app.parser import ValuationParser
from app.parser.excel_reader import WorksheetData
from app.parser.interface import ParsedValuation
from app.validation.rules import check_position_market_value


def _write_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "估值表"
    sheet.append(["证券投资基金估值表"])
    sheet.append(["未知产品___专用表"])
    sheet.append(["估值日期：2026-08-25"])
    sheet.append(
        [
            "科目代码",
            "科目名称",
            "数量",
            "单位成本",
            "成本",
            "成本占净值%",
            "市价",
            "市值",
            "市值占净值%",
            "估值增值",
            "停牌信息",
        ]
    )
    sheet.append(["1002", "银行存款", "", "", 100, 1, "", 100, 1, "", ""])
    sheet.append(["资产类合计", 100])
    sheet.append(["负债类合计", 0])
    sheet.append(["基金资产净值", 100])
    sheet.append(["基金单位净值", 1])
    workbook.save(path)


KNOWN_PRODUCTS = {
    "千帆一号": ["千帆一号"],
    "天衡上将": ["天衡上将"],
    "梦溪一号": ["梦溪一号"],
}


def test_unknown_product_is_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "unknown.xlsx"
    _write_workbook(path)

    parsed = ValuationParser(KNOWN_PRODUCTS).parse(path)
    assert parsed.product_name is None
    assert "product_unrecognized" in parsed.warnings


def test_position_metadata_comes_only_from_explicit_ancestor_names(
    tmp_path: Path,
) -> None:
    path = tmp_path / "position-metadata.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "估值表"
    sheet.append(["证券投资基金估值表"])
    sheet.append(["未知产品___专用表"])
    sheet.append(["估值日期：2026-08-25"])
    sheet.append(
        [
            "科目代码",
            "科目名称",
            "数量",
            "单位成本",
            "成本",
            "成本占净值%",
            "市价",
            "市值",
            "市值占净值%",
            "估值增值",
            "停牌信息",
        ]
    )
    sheet.append(["1102", "股票投资"])
    sheet.append(["110201", "上交所_信用账户"])
    sheet.append(["11020101", "股票成本_上交所_信用账户"])
    sheet.append(["11020101600001", "明确证券", 10, 10, 100, 10, 11, 110, 11, 10, ""])
    sheet.append(["1103", "普通股票投资"])
    sheet.append(
        [
            "11030101600002",
            "深交所信用账户测试证券",
            10,
            10,
            100,
            10,
            11,
            110,
            11,
            10,
            "",
        ]
    )
    sheet.append(["1104", "普通股票投资"])
    sheet.append(["110401", "上海市场策略"])
    sheet.append(["11040101", "多层股票成本"])
    sheet.append(["11040101600003", "多层证券", 10, 10, 100, 10, 11, 110, 11, 10, ""])
    sheet.append(["资产类合计", "", "", "", "", "", "", 330])
    sheet.append(["负债类合计", "", "", "", "", "", "", 0])
    sheet.append(["基金资产净值", "", "", "", "", "", "", 330])
    sheet.append(["基金单位净值", 1])
    workbook.save(path)

    parsed = ValuationParser().parse(path)
    positions = {item.source_subject_code: item for item in parsed.positions}

    explicit = positions["11020101600001"]
    assert explicit.market == "上交所"
    assert explicit.account == "信用账户"
    assert explicit.source_row == 8
    # Long numeric codes must not be truncated to 6 digits, otherwise distinct
    # securities collide on the PositionDaily security_code key.
    assert explicit.security_code == "11020101600001"

    leaf_name_only = positions["11030101600002"]
    assert leaf_name_only.market is None
    assert leaf_name_only.account is None

    ambiguous_ancestor = positions["11040101600003"]
    assert ambiguous_ancestor.market is None
    assert ambiguous_ancestor.account is None


def _parse_position(
    monkeypatch: pytest.MonkeyPatch, cells: dict[str, object]
) -> ParsedValuation:
    worksheet = WorksheetData(
        "估值表",
        (
            ("估值日期：2026-08-25",),
            ("科目代码", "科目名称", *cells),
            ("600001", "测试证券", *cells.values()),
        ),
    )
    monkeypatch.setattr(
        "app.parser.valuation_parser.read_workbook", lambda _path: (worksheet,)
    )
    return ValuationParser().parse(Path("position.xlsx"))


@pytest.mark.parametrize(
    "cells",
    [
        {
            "数量": 10,
            "单位成本": 10,
            "成本": 100,
            "成本占净值%": 25,
            "市价": 11,
            "市值": 110,
            "市值占净值%": 27.5,
        },
        {
            "市值占净值%": 27.5,
            "成本占净值%": 25,
            "单位成本": 10,
            "市值": 110,
            "成本": 100,
            "数量": 10,
            "市价": 11,
        },
        {
            "数量（股）": 10,
            "单位成本（元）": 10,
            "成本金额（元）": 100,
            "成本占净值（%）": 25,
            "市价（元）": 11,
            "市值（元）": 110,
            "市值占净值（%）": 27.5,
        },
    ],
)
def test_position_columns_keep_amounts_distinct_from_units_and_weights(
    monkeypatch: pytest.MonkeyPatch, cells: dict[str, object]
) -> None:
    parsed = _parse_position(monkeypatch, cells)

    assert parsed.subjects[0].cost == Decimal(100)
    position = parsed.positions[0]
    assert position.cost == Decimal(100)
    assert position.unit_cost == Decimal(10)
    assert position.market_value == Decimal(110)
    assert position.market_price == Decimal(11)
    assert parsed.subjects[0].cost_weight == Decimal("0.25")
    assert position.nav_weight == Decimal("0.275")


def test_missing_total_cost_does_not_reuse_unit_cost_or_weight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _parse_position(
        monkeypatch,
        {"数量": 10, "单位成本": 12, "成本占净值%": 25, "市价": 11, "市值": 110},
    )

    assert parsed.subjects[0].cost is None
    assert parsed.positions[0].cost is None
    assert parsed.positions[0].unit_cost == Decimal(12)


@pytest.mark.parametrize("quantity", [10, 0])
def test_source_price_discrepancy_reaches_independent_validation(
    monkeypatch: pytest.MonkeyPatch, quantity: int
) -> None:
    parsed = _parse_position(
        monkeypatch,
        {"数量": quantity, "单位成本": 12, "成本": 100, "市价": 99, "市值": 110},
    )
    position = parsed.positions[0]

    assert position.market_price == Decimal(99)
    assert position.unit_cost == Decimal(12)
    finding = check_position_market_value(position)
    assert finding.level == ValidationLevel.WARNING
    assert finding.difference == Decimal(quantity * 99 - 110)


def test_missing_source_price_is_not_a_successful_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = _parse_position(monkeypatch, {"数量": 10, "成本": 100, "市值": 110})
    position = parsed.positions[0]

    assert position.market_price is None
    finding = check_position_market_value(position)
    assert finding.actual_value is None
    assert finding.difference is None
    assert "暂不执行" in finding.message
