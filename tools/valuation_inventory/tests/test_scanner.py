"""scanner 测试：空目录、混合分类、哈希、中文路径、损坏隔离、--no-parse-xls、源目录不变。"""

from __future__ import annotations

import hashlib
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from tools.valuation_inventory import scanner
from tools.valuation_inventory.excel_metadata import ProductCatalog
from tools.valuation_inventory.models import (
    ErrorType,
    FileType,
    ParseStatus,
    SourceZone,
)
from tools.valuation_inventory.tests.conftest import (
    make_transaction_xlsx,
    make_valuation_xlsx,
)


def scan_dir(root: Path, **kw):
    return scanner.scan(root, scanner.ScanOptions(**kw))


class TestBasicScan:
    def test_empty_dir(self, tmp_path):
        root = tmp_path / "empty"
        root.mkdir()
        result = scan_dir(root)
        assert result.files == []
        assert result.root_name == "empty"

    def test_mixed_classification(self, tmp_path):
        root = tmp_path / "估值表A"
        v_dir = root / "梦溪一号估值表" / "2026年01月"
        tx_dir = root / "千帆一号估值表" / "交易记录备份"
        v_dir.mkdir(parents=True)
        tx_dir.mkdir(parents=True)
        make_valuation_xlsx(
            v_dir / "梦溪一号 01月05日.xlsx", date_text="估值日期：20260105"
        )
        make_transaction_xlsx(root / "千帆一号估值表" / "千帆一号_交易记录.xlsx")
        make_transaction_xlsx(
            tx_dir / "_backup_before_千帆一号_2026-01-05.xlsx", live=False
        )
        (root / "千帆一号估值表" / "千帆一号_交易记录.xlsx.bak_cum_nav").write_bytes(
            b"x"
        )
        (root / "说明.txt").write_text("note", encoding="utf-8")

        result = scan_dir(root)
        by_rel = {f.rel_path: f for f in result.files}
        assert len(result.files) == 5

        v = by_rel["梦溪一号估值表/2026年01月/梦溪一号 01月05日.xlsx"]
        assert v.zone is SourceZone.PRIMARY
        assert v.file_type is FileType.VALUATION_XLSX
        assert v.is_valuation
        assert v.product == "梦溪一号"
        assert v.valuation_date == date(2026, 1, 5)
        assert v.header_row == 4
        assert v.parse_status is ParseStatus.OK
        assert datetime.fromisoformat(v.mtime).tzinfo is not None

        live = by_rel["千帆一号估值表/千帆一号_交易记录.xlsx"]
        assert live.file_type is FileType.TRANSACTION_XLSX
        assert not live.is_valuation
        assert live.product == "千帆一号"  # 路径+文件名仍可给出候选产品

        backup = by_rel[
            "千帆一号估值表/交易记录备份/_backup_before_千帆一号_2026-01-05.xlsx"
        ]
        assert backup.file_type is FileType.TRANSACTION_XLSX
        assert backup.zone is SourceZone.PRIMARY

        sidecar = by_rel["千帆一号估值表/千帆一号_交易记录.xlsx.bak_cum_nav"]
        assert sidecar.file_type is FileType.BACKUP_SIDECAR
        assert sidecar.parse_status is ParseStatus.NOT_APPLICABLE
        assert sidecar.sha256  # 旁车文件仍计算哈希

        txt = by_rel["说明.txt"]
        assert txt.file_type is FileType.UNKNOWN
        assert txt.zone is SourceZone.OTHER

    def test_sha256_matches_hashlib(self, tmp_path):
        root = tmp_path / "r"
        root.mkdir()
        p = root / "梦溪一号估值表" / "a.xlsx"
        p.parent.mkdir()
        make_valuation_xlsx(p)
        result = scan_dir(root)
        expected = hashlib.sha256(p.read_bytes()).hexdigest()
        assert result.files[0].sha256 == expected

    def test_chinese_path_and_filename(self, tmp_path):
        root = tmp_path / "估值表A"
        deep = root / "梦溪一号估值表" / "2026年01-12月" / "2026年01月"
        deep.mkdir(parents=True)
        make_valuation_xlsx(
            deep / "梦溪一号 01月05日.xlsx", date_text="估值日期：20260105"
        )
        result = scan_dir(root)
        f = result.files[0]
        assert (
            f.rel_path
            == "梦溪一号估值表/2026年01-12月/2026年01月/梦溪一号 01月05日.xlsx"
        )
        assert f.is_valuation and f.valuation_date == date(2026, 1, 5)

    def test_gz_zone(self, tmp_path):
        root = tmp_path / "估值表A"
        gz = root / "gz" / "梦溪一号估值表"
        gz.mkdir(parents=True)
        make_valuation_xlsx(gz / "梦溪一号 01月05日.xlsx", sheet_name="Sheet1")
        result = scan_dir(root)
        f = result.files[0]
        assert f.zone is SourceZone.GZ
        # gz 文件 sheet 名是 Sheet1 时，产品仍可由路径识别
        assert f.product == "梦溪一号"


class TestDateFormats:
    def test_supported_formats(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        cases = {
            "梦溪一号 04月02日.xlsx": "估值日期：20260402",
            "梦溪一号 04月03日.xlsx": "估值日期：2026-04-03",
            "梦溪一号 04月06日.xlsx": "估值日期：2026/04/06",
        }
        for name, text in cases.items():
            make_valuation_xlsx(d / name, date_text=text)
        result = scan_dir(root)
        dates = sorted(f.valuation_date for f in result.files)
        assert dates == [date(2026, 4, 2), date(2026, 4, 3), date(2026, 4, 6)]

    def test_unrecognized_date(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        make_valuation_xlsx(
            d / "梦溪一号 01月05日.xlsx", date_text="净值是否确认：已确认"
        )
        result = scan_dir(root)
        f = result.files[0]
        assert f.is_valuation
        assert f.valuation_date is None

    def test_unrecognized_product(self, tmp_path):
        root = tmp_path / "未知来源"
        root.mkdir()
        make_valuation_xlsx(
            root / "某产品 01月05日.xlsx",
            title="某私募证券投资基金",  # 标题不含已知产品别名
            date_text="估值日期：20260105",
        )
        result = scan_dir(root)
        f = result.files[0]
        assert f.is_valuation
        assert f.product is None
        assert f.product_candidates == []
        assert f.zone is SourceZone.OTHER


class TestFaultIsolation:
    def test_corrupt_xls_does_not_stop_scan(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        (d / "坏文件 01月01日.xls").write_bytes(b"this is not excel")
        make_valuation_xlsx(d / "好文件 01月05日.xlsx", date_text="估值日期：20260105")
        result = scan_dir(root)
        by_rel = {f.rel_path: f for f in result.files}
        bad = by_rel["梦溪一号估值表/坏文件 01月01日.xls"]
        good = by_rel["梦溪一号估值表/好文件 01月05日.xlsx"]
        assert bad.parse_status is ParseStatus.FAILED
        assert bad.error_type.value == "read_error"
        assert bad.error_message
        assert bad.sha256  # 哈希仍可计算（按字节）
        assert not bad.is_valuation
        assert good.parse_status is ParseStatus.OK
        assert good.is_valuation

    def test_corrupt_xlsx_does_not_stop_scan(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        (d / "坏 01月02日.xlsx").write_bytes(b"PK\x03\x04 broken")
        make_valuation_xlsx(d / "好 01月05日.xlsx")
        result = scan_dir(root)
        bad = next(f for f in result.files if f.file_name.startswith("坏"))
        assert bad.parse_status is ParseStatus.FAILED
        assert str(root.resolve()) not in bad.error_message
        assert len(result.files) == 2

    def test_parse_xls_disabled(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        make_valuation_xlsx(d / "梦溪一号 01月05日.xlsx")
        (d / "梦溪一号 01月06日.xls").write_bytes(b"fake-xls-bytes")
        result = scan_dir(root, parse_xls=False)
        xls = next(f for f in result.files if f.ext == ".xls")
        assert xls.parse_status is ParseStatus.NOT_APPLICABLE
        assert "no-parse-xls" in xls.error_message
        assert xls.file_type is FileType.VALUATION_XLS  # 仅按扩展名的初步分类
        assert not xls.is_valuation
        assert xls.sha256
        xlsx = next(f for f in result.files if f.ext == ".xlsx")
        assert xlsx.parse_status is ParseStatus.OK  # xlsx 解析不受该开关影响

    def test_workers_parallel_equals_serial(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        for i in range(5):
            make_valuation_xlsx(
                d / f"梦溪一号 01月0{i + 1}日.xlsx",
                date_text=f"估值日期：2026010{i + 1}",
            )
        serial = scan_dir(root)
        parallel = scan_dir(root, workers=4)
        assert [f.rel_path for f in serial.files] == [
            f.rel_path for f in parallel.files
        ]
        assert [f.sha256 for f in serial.files] == [f.sha256 for f in parallel.files]

    def test_parallel_progress_counts_in_order(self, tmp_path):
        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表"
        d.mkdir(parents=True)
        for i in range(24):
            (d / f"{i:02d}.txt").write_text(str(i), encoding="utf-8")

        calls: list[tuple[int, int]] = []

        def progress(done: int, total: int) -> None:
            if done == 1:
                time.sleep(0.01)
            calls.append((done, total))

        result = scanner.scan(root, scanner.ScanOptions(workers=8), progress=progress)

        assert [done for done, _ in calls] == list(range(1, len(result.files) + 1))
        assert {total for _, total in calls} == {len(result.files)}


class TestPathSafety:
    def test_symlink_files_and_directories_are_skipped(self, tmp_path):
        root = tmp_path / "估值表A"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        make_valuation_xlsx(outside / "secret.xlsx")
        (root / "inside.txt").write_text("inside", encoding="utf-8")

        try:
            (root / "linked.txt").symlink_to(outside / "secret.txt")
            (root / "linked-dir").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"symlink unavailable: {e}")

        result = scan_dir(root)

        assert [f.rel_path for f in result.files] == ["inside.txt"]

    def test_scan_file_rejects_path_resolving_outside_root(self, tmp_path):
        root = tmp_path / "估值表A"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        make_valuation_xlsx(outside / "secret.xlsx")
        link = root / "linked.xlsx"
        try:
            link.symlink_to(outside / "secret.xlsx")
        except (OSError, NotImplementedError) as e:
            pytest.skip(f"symlink unavailable: {e}")

        info = scanner.scan_file(
            root, "linked.xlsx", scanner.ScanOptions(), ProductCatalog()
        )

        assert info.parse_status is ParseStatus.FAILED
        assert info.error_type is ErrorType.READ_ERROR
        assert info.sha256 is None
        assert str(outside.resolve()) not in info.error_message


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """源目录快照：相对路径 → (大小, mtime_ns)。"""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return snap


class TestReadOnlyGuarantee:
    def test_source_directory_unchanged_after_scan_and_reports(self, tmp_path):
        from tools.valuation_inventory import dedup, report

        root = tmp_path / "估值表A"
        d = root / "梦溪一号估值表" / "2026年01月"
        d.mkdir(parents=True)
        make_valuation_xlsx(d / "梦溪一号 01月05日.xlsx")
        make_valuation_xlsx(
            d / "梦溪一号 01月06日.xlsx", date_text="估值日期：20260106"
        )

        before = snapshot(root)
        result = scan_dir(root)
        dedup_result = dedup.analyze(result.files)
        out = tmp_path / "out" / "valuation-inventory"
        report.write_reports(
            out, result.files, dedup_result, report.ReportConfig(root_name=root.name)
        )
        after = snapshot(root)

        assert before == after  # 无新增、无删除、无移动、无修改
        assert out.is_dir() and len(list(out.iterdir())) == 5
