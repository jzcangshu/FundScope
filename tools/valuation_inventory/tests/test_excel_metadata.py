"""excel_metadata 单元测试：日期解析、表头定位、估值表判定、产品识别、xlrd 适配器。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import xlrd

from tools.valuation_inventory import excel_metadata as em
from tools.valuation_inventory.tests.conftest import (
    FakeBook,
    FakeSheet,
    make_valuation_xlsx,
)


def grid_of(rows, name="Sheet1") -> em.SheetGrid:
    return em.SheetGrid(name=name, rows=rows)


def valuation_rows(date_cell, header_row=4):
    rows = [
        ["证券投资基金估值表"],
        ["上海示例___梦溪一号___专用表"],
        [date_cell],
    ]
    rows += [[] for _ in range(header_row - 4)]
    rows.append(
        [
            "科目代码",
            "科目名称",
            "数量",
            "单位成本",
            "成本",
            "成本占净值%",
            "市价",
            "市值",
        ]
    )
    rows.append(["1002", "银行存款", None, None, 640251.73, None, None, 640251.73])
    rows.append(["基金资产净值:", None, None, None, 131532087.37])
    rows.append(["基金单位净值：", 2.5882])
    rows.append(["累计单位净值:", 3.5079])
    return rows


class TestParseDateText:
    def test_compact(self):
        assert em.parse_date_text("估值日期：20260402") == date(2026, 4, 2)

    def test_dash(self):
        assert em.parse_date_text("估值日期: 2026-04-02") == date(2026, 4, 2)

    def test_slash(self):
        assert em.parse_date_text("2026/04/02") == date(2026, 4, 2)

    def test_chinese(self):
        assert em.parse_date_text("2026年4月2日") == date(2026, 4, 2)

    def test_invalid_month(self):
        assert em.parse_date_text("20261301") is None

    def test_plain_code_not_matched(self):
        assert em.parse_date_text("100201") is None

    def test_empty(self):
        assert em.parse_date_text("") is None


class TestExtractFacts:
    def test_header_found_by_content_not_fixed_row(self):
        rows = valuation_rows("估值日期：20260402", header_row=6)
        facts = em.extract_facts(grid_of(rows))
        assert facts.header_row == 6
        assert facts.is_valuation

    def test_valuation_detection_requires_both_headers(self):
        rows = valuation_rows("估值日期：20260402")
        rows[3] = ["科目代码", "科目"]  # 缺“科目名称”
        facts = em.extract_facts(grid_of(rows))
        assert not facts.is_valuation
        assert facts.header_row is None

    def test_valuation_detection_via_nav_without_date_label(self):
        rows = valuation_rows("净值是否确认：已确认")
        facts = em.extract_facts(grid_of(rows))
        assert facts.valuation_date is None
        assert facts.is_valuation  # 命中基金资产净值/基金单位净值/累计单位净值

    def test_neither_date_nor_nav_not_valuation(self):
        rows = valuation_rows("净值是否确认：已确认")
        for i, row in enumerate(rows):
            if (
                row
                and isinstance(row[0], str)
                and row[0].startswith(("基金资产净值", "基金单位净值", "累计单位净值"))
            ):
                rows[i] = []
        facts = em.extract_facts(grid_of(rows))
        assert not facts.is_valuation

    def test_date_from_label_cell(self):
        facts = em.extract_facts(grid_of(valuation_rows("估值日期：2026-04-02")))
        assert facts.valuation_date == date(2026, 4, 2)

    def test_date_from_right_neighbor_datetime(self):
        rows = valuation_rows("净值是否确认：已确认")
        rows[2] = [
            "估值日期：",
            datetime(2026, 4, 2, tzinfo=timezone.utc),
        ]  # 标签与日期分居两个单元格
        facts = em.extract_facts(grid_of(rows))
        assert facts.valuation_date == date(2026, 4, 2)

    def test_date_from_typed_cell_fallback(self):
        facts = em.extract_facts(
            grid_of(valuation_rows(datetime(2026, 4, 2, tzinfo=timezone.utc)))
        )
        assert facts.valuation_date == date(2026, 4, 2)

    def test_text_number_samples(self):
        rows = valuation_rows("估值日期：20260402")
        rows.append(["示例", "10,824,713.18"])
        rows.append(["示例", "3.74"])
        rows.append(["示例", "-0.9719"])
        rows.append(["代码", "1002"])  # 纯整数代码不算
        facts = em.extract_facts(grid_of(rows))
        assert facts.text_number_count == 3
        assert facts.text_number_samples == ["10,824,713.18", "3.74", "-0.9719"]

    def test_title_lines_before_header(self):
        facts = em.extract_facts(grid_of(valuation_rows("估值日期：20260402")))
        assert any("梦溪一号" in t for t in facts.title_lines)


class TestProductCatalog:
    def test_alias_hit(self):
        cat = em.ProductCatalog()
        assert cat.candidates_from_text("示例梦溪一号私募证券投资基金") == ["梦溪一号"]
        assert cat.candidates_from_text("Sheet1") == []

    def test_dir_suffix_rule(self):
        cat = em.ProductCatalog()
        assert cat.candidates_from_dir_name("梦溪一号估值表") == ["梦溪一号"]
        assert cat.candidates_from_dir_name("2026年01-12月") == []
        # 未知新产品目录也能给出候选（可扩展规则）
        assert cat.candidates_from_dir_name("新星二号估值表") == ["新星二号"]

    def test_alias_preferred_over_suffix(self):
        cat = em.ProductCatalog()
        assert cat.candidates_from_dir_name("天衡上将估值表") == ["天衡上将"]

    def test_resolve_unique_and_conflict(self):
        cat = em.ProductCatalog()
        assert cat.resolve(["梦溪一号", "梦溪一号"]) == ("梦溪一号", False)
        assert cat.resolve(["梦溪一号", "千帆一号"]) == (None, True)
        assert cat.resolve([]) == (None, False)


class TestXlrdAdapter:
    def test_load_grids_from_fake_xlrd(self, tmp_path: Path, monkeypatch):
        serial = (date(2026, 4, 2) - date(1899, 12, 30)).days
        sheet = FakeSheet(
            name="估值表",
            rows=[
                [("证券投资基金估值表", 0)],
                [("估值日期：", 0), (serial, xlrd.XL_CELL_DATE)],
                [("科目代码", 0), ("科目名称", 0)],
                [("1002.0", 0)],
            ],
        )
        monkeypatch.setattr(
            xlrd, "open_workbook", lambda path: FakeBook([sheet], datemode=0)
        )
        grids = em._grids_from_xlrd(tmp_path / "fake.xls")
        assert len(grids) == 1
        assert grids[0].name == "估值表"
        assert grids[0].rows[1][1].date() == date(2026, 4, 2)
        facts = em.extract_facts(grids[0])
        assert facts.valuation_date == date(2026, 4, 2)


class TestOpenpyxlAdapter:
    def test_load_grids_real_xlsx(self, tmp_path: Path):
        p = tmp_path / "梦溪一号 01月05日.xlsx"
        make_valuation_xlsx(p, date_text="估值日期：20260105")
        grids = em.load_grids(p)
        assert len(grids) == 1
        facts = em.analyze_grids(grids)
        assert facts.is_valuation
        assert facts.valuation_date == date(2026, 1, 5)
        assert facts.facts is not None and facts.facts.header_row == 4
