"""dedup 单元测试：重复、冲突、primary/gz 覆盖差异、身份未识别、同名跨年份。"""

from __future__ import annotations

from datetime import date

from tools.valuation_inventory import dedup as dd
from tools.valuation_inventory.models import SourceZone
from tools.valuation_inventory.tests.conftest import make_file_info

D1 = date(2026, 1, 5)
D2 = date(2026, 1, 6)
SHA_A = "a" * 64
SHA_B = "b" * 64


class TestDuplicatesAndConflicts:
    def test_same_content_duplicate_within_primary(self):
        files = [
            make_file_info(
                "梦溪一号估值表/2026年01月/a.xls", valuation_date=D1, sha256=SHA_A
            ),
            make_file_info(
                "梦溪一号估值表/2026年01月/b.xls", valuation_date=D1, sha256=SHA_A
            ),
        ]
        result = dd.analyze(files)
        assert len(result.groups) == 1
        g = result.groups[0]
        assert g.classification == dd.CLASS_SAME_CONTENT
        assert len(g.members) == 2
        assert g.keep == "梦溪一号估值表/2026年01月/a.xls"

    def test_same_date_conflict(self):
        files = [
            make_file_info(
                "梦溪一号估值表/2026年01月/a.xls", valuation_date=D1, sha256=SHA_A
            ),
            make_file_info(
                "梦溪一号估值表/2026年01月/b.xls", valuation_date=D1, sha256=SHA_B
            ),
        ]
        result = dd.analyze(files)
        g = result.groups[0]
        assert g.classification == dd.CLASS_SAME_DATE_CONFLICT
        assert g.keep is None  # 冲突不自动选择
        assert result.stats["conflict_group_count"] == 1
        assert result.stats["conflict_file_count"] == 2

    def test_single_file_no_group(self):
        files = [
            make_file_info("梦溪一号估值表/a.xls", valuation_date=D1, sha256=SHA_A),
            make_file_info("梦溪一号估值表/b.xls", valuation_date=D2, sha256=SHA_A),
            make_file_info(
                "千帆一号估值表/a.xls",
                product="千帆一号",
                valuation_date=D1,
                sha256=SHA_A,
            ),
        ]
        result = dd.analyze(files)
        # 哈希相同但产品或表内估值日不同，不构成重复组。
        assert result.groups == []


class TestZoneComparison:
    def test_cross_zone_same_hash_is_duplicate_with_primary_kept(self):
        files = [
            make_file_info(
                "梦溪一号估值表/2026年01月/梦溪一号 01月05日.xls",
                zone=SourceZone.PRIMARY,
                valuation_date=D1,
                sha256=SHA_A,
            ),
            make_file_info(
                "gz/梦溪一号估值表/2026年01月/梦溪一号 01月05日.xls",
                zone=SourceZone.GZ,
                valuation_date=D1,
                sha256=SHA_A,
            ),
        ]
        result = dd.analyze(files)
        g = result.groups[0]
        assert g.classification == dd.CLASS_SAME_CONTENT
        assert g.keep == "梦溪一号估值表/2026年01月/梦溪一号 01月05日.xls"
        assert result.stats["primary_only_count"] == 0
        assert result.stats["gz_only_count"] == 0

    def test_cross_zone_same_date_different_hash_is_conflict(self):
        files = [
            make_file_info(
                "梦溪一号估值表/2026年01月/梦溪一号 01月06日.xls",
                zone=SourceZone.PRIMARY,
                valuation_date=D1,
                sha256=SHA_A,
            ),
            make_file_info(
                "gz/梦溪一号估值表/2026年01月/梦溪一号 01月06日.xls",
                zone=SourceZone.GZ,
                valuation_date=D1,
                sha256=SHA_B,
            ),
        ]
        result = dd.analyze(files)
        g = result.groups[0]
        assert g.classification == dd.CLASS_SAME_DATE_CONFLICT
        assert g.keep is None
        assert result.primary_only == [] and result.gz_only == []

    def test_primary_only_and_gz_only(self):
        files = [
            make_file_info(
                "梦溪一号估值表/2026年01月/a.xls",
                zone=SourceZone.PRIMARY,
                valuation_date=D1,
            ),
            make_file_info(
                "gz/梦溪一号估值表/2026年01月/b.xls",
                zone=SourceZone.GZ,
                valuation_date=D2,
            ),
        ]
        result = dd.analyze(files)
        assert [e.as_dict() for e in result.primary_only] == [
            {
                "product": "梦溪一号",
                "valuation_date": D1.isoformat(),
                "rel_path": "梦溪一号估值表/2026年01月/a.xls",
            }
        ]
        assert [e.as_dict() for e in result.gz_only] == [
            {
                "product": "梦溪一号",
                "valuation_date": D2.isoformat(),
                "rel_path": "gz/梦溪一号估值表/2026年01月/b.xls",
            }
        ]

    def test_same_name_cross_zone_uses_sheet_date_not_filename(self):
        # 同名文件跨年份放置：主目录是 2025 年，gz 是 2026 年，表内日期不同 → 非重复
        files = [
            make_file_info(
                "梦溪一号估值表/2025年01-12月/梦溪一号 01月05日.xls",
                zone=SourceZone.PRIMARY,
                valuation_date=date(2025, 1, 6),
                sha256=SHA_A,
            ),
            make_file_info(
                "gz/梦溪一号估值表/2026年01-12月/梦溪一号 01月05日.xls",
                zone=SourceZone.GZ,
                valuation_date=date(2026, 1, 5),
                sha256=SHA_B,
            ),
        ]
        result = dd.analyze(files)
        assert result.groups == []  # 日期不同，不构成重复或冲突
        assert result.primary_only[0].valuation_date == date(2025, 1, 6)
        assert result.gz_only[0].valuation_date == date(2026, 1, 5)
        pair = result.same_name_cross_zone[0]
        assert pair.file_name == "梦溪一号 01月05日.xls"
        assert pair.hash_equal is False
        assert pair.primary_date == date(2025, 1, 6)
        assert pair.gz_date == date(2026, 1, 5)


class TestUnresolved:
    def test_no_product_reason(self):
        files = [
            make_file_info(
                "未知/a.xls", zone=SourceZone.OTHER, product=None, valuation_date=D1
            )
        ]
        result = dd.analyze(files)
        assert result.unresolved[0].reason == dd.REASON_NO_PRODUCT

    def test_no_date_reason(self):
        files = [
            make_file_info("梦溪一号估值表/a.xls", valuation_date=None),
            make_file_info("梦溪一号估值表/b.xls", valuation_date=D1, sha256=None),
        ]
        result = dd.analyze(files)
        reasons = {entry.rel_path: entry.reason for entry in result.unresolved}
        assert reasons == {
            "梦溪一号估值表/a.xls": dd.REASON_NO_DATE,
            "梦溪一号估值表/b.xls": dd.REASON_NO_HASH,
        }

    def test_identity_conflict_reason(self):
        files = [
            make_file_info(
                "梦溪一号估值表/a.xls",
                product=None,
                valuation_date=D1,
                identity_conflict=True,
            )
        ]
        result = dd.analyze(files)
        assert result.unresolved[0].reason == dd.REASON_IDENTITY_CONFLICT

    def test_non_valuation_files_excluded(self):
        files = [
            make_file_info(
                "千帆一号估值表/交易记录.xlsx",
                is_valuation=False,
                valuation_date=None,
                product=None,
            )
        ]
        result = dd.analyze(files)
        assert result.groups == []
        assert result.unresolved == []  # 非估值表不参与去重
        assert result.stats["valuation_files"] == 0
