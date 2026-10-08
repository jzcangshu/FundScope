"""report 与 cli 测试：三种格式报告、确定性排序、无绝对路径泄露、迁移清单动作。"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from tools.valuation_inventory.cli import main
from tools.valuation_inventory.tests.conftest import build_synth_tree

ALL_REPORTS = [
    "inventory.json",
    "inventory.csv",
    "summary.md",
    "dedup-groups.json",
    "migration-candidates.csv",
]


def run_cli(root: Path, out: Path, *extra: str) -> int:
    return main(["--root", str(root), "--out", str(out), *extra])


class TestCliEndToEnd:
    def test_full_run_generates_all_reports(self, tmp_path):
        root = build_synth_tree(tmp_path)
        out = tmp_path / "artifacts" / "valuation-inventory"
        code = run_cli(root, out, "--format", "all")
        assert code == 0
        names = sorted(p.name for p in out.iterdir())
        assert names == sorted(ALL_REPORTS)

    def test_json_only_format(self, tmp_path):
        root = build_synth_tree(tmp_path)
        out = tmp_path / "out-json"
        assert run_cli(root, out, "--format", "json") == 0
        assert sorted(p.name for p in out.iterdir()) == [
            "dedup-groups.json",
            "inventory.json",
        ]

    def test_csv_only_format(self, tmp_path):
        root = build_synth_tree(tmp_path)
        out = tmp_path / "out-csv"
        assert run_cli(root, out, "--format", "csv") == 0
        assert sorted(p.name for p in out.iterdir()) == [
            "inventory.csv",
            "migration-candidates.csv",
        ]

    def test_missing_root_returns_error(self, tmp_path):
        code = run_cli(tmp_path / "不存在", tmp_path / "out")
        assert code == 2

        root = build_synth_tree(tmp_path)
        out = root / "artifacts"
        assert run_cli(root, out) == 2
        assert not out.exists()

    def test_invalid_workers(self, tmp_path):
        root = build_synth_tree(tmp_path)
        assert run_cli(root, tmp_path / "o", "--workers", "0") == 2


class TestReportContent:
    @pytest.fixture()
    def artifacts(self, tmp_path):
        root = build_synth_tree(tmp_path)
        out = tmp_path / "out"
        run_cli(root, out)
        return root, out

    def test_inventory_json_structure(self, artifacts):
        _, out = artifacts
        payload = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
        assert payload["schema_version"] == "1"
        assert payload["root"] == "估值表A"
        files = payload["files"]
        assert len(files) == 9
        rels = [f["rel_path"] for f in files]
        assert rels == sorted(rels)  # 默认按相对路径排序
        for f in files:
            assert not f["rel_path"].startswith(("/", "\\"))
            assert ":" not in f["rel_path"]
        s = payload["summary"]
        assert s["total_files"] == 9
        assert s["valuation_count"] == 5
        assert s["non_valuation_count"] == 4
        assert s["valuation_date_min"] == "2026-01-05"
        assert s["valuation_date_max"] == "2026-01-07"

    def test_inventory_csv_rows(self, artifacts):
        _, out = artifacts
        with (out / "inventory.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 9
        assert rows[0]["rel_path"] <= rows[-1]["rel_path"]

    def test_summary_md_sections(self, artifacts):
        _, out = artifacts
        md = (out / "summary.md").read_text(encoding="utf-8")
        for section in [
            "# 历史估值文件盘点汇总",
            "## 按扩展名统计",
            "## 按来源区域统计",
            "## 按产品统计（估值表）",
            "## 去重与冲突",
            "## 数据质量问题计数",
            "## 每个产品的日期覆盖与缺口",
            "## 需要人工复核",
            "同产品同日期内容冲突",
            "读取/解析失败",
        ]:
            assert section in md
        assert "梦溪一号" in md

    def test_dedup_groups_json(self, artifacts):
        _, out = artifacts
        payload = json.loads((out / "dedup-groups.json").read_text(encoding="utf-8"))
        classes = {g["classification"] for g in payload["groups"]}
        assert classes == {"same_content_duplicate", "same_date_conflict"}
        assert payload["stats"]["gz_only_count"] == 1
        assert payload["stats"]["primary_only_count"] == 0
        assert payload["same_name_cross_zone"]  # 同名跨区域对已列出

    def test_no_absolute_path_leak(self, artifacts):
        root, out = artifacts
        leaked = str(root.resolve())
        for p in out.iterdir():
            text = p.read_text(encoding="utf-8-sig")
            assert leaked not in text, f"{p.name} 泄露绝对路径"
            assert "F:\\\\" not in text and "F:/" not in text

    def test_deterministic_ordering(self, tmp_path):
        root = build_synth_tree(tmp_path)
        out1, out2 = tmp_path / "r1", tmp_path / "r2"
        run_cli(root, out1)
        run_cli(root, out2)
        p1 = json.loads((out1 / "inventory.json").read_text(encoding="utf-8"))
        p2 = json.loads((out2 / "inventory.json").read_text(encoding="utf-8"))
        assert p1["files"] == p2["files"]
        assert p1["summary"] == p2["summary"]
        d1 = json.loads((out1 / "dedup-groups.json").read_text(encoding="utf-8"))
        d2 = json.loads((out2 / "dedup-groups.json").read_text(encoding="utf-8"))
        assert d1 == d2
        assert (out1 / "inventory.csv").read_bytes() == (
            out2 / "inventory.csv"
        ).read_bytes()
        assert (out1 / "migration-candidates.csv").read_bytes() == (
            out2 / "migration-candidates.csv"
        ).read_bytes()

    def test_read_failures_recorded(self, artifacts):
        _, out = artifacts
        with (out / "inventory.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        failed = [r for r in rows if r["parse_status"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["file_name"] == "梦溪一号 01月08日.xls"
        assert failed[0]["error_type"] == "read_error"
        assert failed[0]["error_message"]

    def test_conflict_not_auto_chosen_in_migration_csv(self, artifacts):
        _, out = artifacts
        with (out / "migration-candidates.csv").open(
            encoding="utf-8-sig", newline=""
        ) as fh:
            rows = list(csv.DictReader(fh))
        by_action = {}
        for r in rows:
            by_action.setdefault(r["action"], []).append(r)
        # 冲突双方都是 needs_review，未被自动选择。
        conflict_rows = [r for r in rows if "same_date_conflict" in r["note"]]
        assert {r["rel_path"] for r in conflict_rows} == {
            "梦溪一号估值表/2026年01-12月/2026年01月/梦溪一号 01月06日.xlsx",
            "gz/梦溪一号估值表/2026年01-12月/2026年01月/梦溪一号 01月06日.xlsx",
        }
        assert len(conflict_rows) == 2
        assert all(r["action"] == "needs_review" for r in conflict_rows)
        # gz 独有日期 → 补充候选
        assert any(r["action"] == "import_gz_only" for r in rows)
        # gz 与主目录同哈希 → skip_duplicate 且指向保留文件
        skips = by_action.get("skip_duplicate", [])
        assert len(skips) == 1
        assert skips[0]["duplicate_of"].startswith("梦溪一号估值表/")
        # 主目录正常文件 → import（冲突组的主目录文件不自动选择，不在此列）
        imports = by_action.get("import", [])
        assert {r["rel_path"] for r in imports} == {
            "梦溪一号估值表/2026年01-12月/2026年01月/梦溪一号 01月05日.xlsx",
        }
        # 交易记录/旁车/损坏文件不进入迁移清单
        valuation_rels = {r["rel_path"] for r in rows}
        assert "千帆一号估值表/千帆一号_交易记录.xlsx" not in valuation_rels

    def test_gap_computation_weekdays_only(self):
        from tools.valuation_inventory.report import weekday_gaps

        # 1月5日(一) → 1月9日(五)：中间 6/7/8 日为工作日缺口
        gaps = weekday_gaps([date(2026, 1, 5), date(2026, 1, 9)])
        assert len(gaps) == 1
        assert [d.isoformat() for d in gaps[0].missing_weekdays] == [
            "2026-01-06",
            "2026-01-07",
            "2026-01-08",
        ]
        # 1月9日(五) → 1月12日(一)：跳过周末，无缺口
        assert weekday_gaps([date(2026, 1, 9), date(2026, 1, 12)]) == []
