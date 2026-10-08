"""去重与冲突分类：按（候选产品, 表内估值日期, 哈希）比较主目录与 gz。

分类口径（与《06-历史数据迁移清单》一致）：
- ``same_content_duplicate``：同一产品、同一估值日期，内容哈希完全相同。
- ``same_date_conflict``：同一产品、同一估值日期，内容哈希不同（不自动选择）。
- ``primary_only`` / ``gz_only``：某产品+日期只在一个来源区域存在。
- ``unresolved_identity``：估值表但产品或日期无法可靠识别。

同名跨年份文件以表内估值日期为准，文件名仅作为辅助比对输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import FileInfo, SourceZone

CLASS_SAME_CONTENT = "same_content_duplicate"
CLASS_SAME_DATE_CONFLICT = "same_date_conflict"

REASON_NO_PRODUCT = "no_product"
REASON_NO_DATE = "no_date"
REASON_NO_HASH = "no_hash"
REASON_IDENTITY_CONFLICT = "identity_conflict"


@dataclass
class GroupMember:
    rel_path: str
    zone: str
    file_name: str
    sha256: str | None
    valuation_date: date | None

    def as_dict(self) -> dict:
        return {
            "rel_path": self.rel_path,
            "zone": self.zone,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "valuation_date": self.valuation_date.isoformat()
            if self.valuation_date
            else None,
        }


@dataclass
class DedupGroup:
    product: str
    valuation_date: date
    classification: str
    members: list[GroupMember] = field(default_factory=list)
    # 冲突组不自动选择；重复组给出“主目录优先、相对路径排序”的保留建议
    keep: str | None = None

    def as_dict(self) -> dict:
        return {
            "product": self.product,
            "valuation_date": self.valuation_date.isoformat(),
            "classification": self.classification,
            "members": [m.as_dict() for m in self.members],
            "keep": self.keep,
        }


@dataclass
class CoverageEntry:
    product: str
    valuation_date: date
    rel_path: str  # 代表文件（区域内按相对路径排序的第一个）

    def as_dict(self) -> dict:
        return {
            "product": self.product,
            "valuation_date": self.valuation_date.isoformat(),
            "rel_path": self.rel_path,
        }


@dataclass
class UnresolvedEntry:
    rel_path: str
    reason: str
    product: str | None
    valuation_date: date | None

    def as_dict(self) -> dict:
        return {
            "rel_path": self.rel_path,
            "reason": self.reason,
            "product": self.product,
            "valuation_date": self.valuation_date.isoformat()
            if self.valuation_date
            else None,
        }


@dataclass
class SameNamePair:
    """跨区域同名文件对：用于核对“同名跨年份放置”的情况。"""

    file_name: str
    primary_rel: str
    gz_rel: str
    hash_equal: bool
    primary_date: date | None
    gz_date: date | None

    def as_dict(self) -> dict:
        return {
            "file_name": self.file_name,
            "primary_rel_path": self.primary_rel,
            "gz_rel_path": self.gz_rel,
            "hash_equal": self.hash_equal,
            "primary_valuation_date": self.primary_date.isoformat()
            if self.primary_date
            else None,
            "gz_valuation_date": self.gz_date.isoformat() if self.gz_date else None,
        }


@dataclass
class DedupResult:
    groups: list[DedupGroup] = field(default_factory=list)
    primary_only: list[CoverageEntry] = field(default_factory=list)
    gz_only: list[CoverageEntry] = field(default_factory=list)
    unresolved: list[UnresolvedEntry] = field(default_factory=list)
    same_name_cross_zone: list[SameNamePair] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _member(info: FileInfo) -> GroupMember:
    return GroupMember(
        rel_path=info.rel_path,
        zone=info.zone.value,
        file_name=info.file_name,
        sha256=info.sha256,
        valuation_date=info.valuation_date,
    )


def _zone_order(m: GroupMember) -> tuple[int, str]:
    # primary 第一来源优先；同区域内按 rel_path 排序
    return (0 if m.zone == SourceZone.PRIMARY.value else 1, m.rel_path)


def analyze(files: list[FileInfo]) -> DedupResult:
    result = DedupResult()

    valuation = [f for f in files if f.is_valuation]
    resolved: list[FileInfo] = []
    for f in valuation:
        if f.identity_conflict:
            result.unresolved.append(
                UnresolvedEntry(
                    f.rel_path, REASON_IDENTITY_CONFLICT, None, f.valuation_date
                )
            )
        elif not f.product:
            result.unresolved.append(
                UnresolvedEntry(f.rel_path, REASON_NO_PRODUCT, None, f.valuation_date)
            )
        elif not f.valuation_date:
            result.unresolved.append(
                UnresolvedEntry(f.rel_path, REASON_NO_DATE, f.product, None)
            )
        elif not f.sha256:
            result.unresolved.append(
                UnresolvedEntry(f.rel_path, REASON_NO_HASH, f.product, f.valuation_date)
            )
        else:
            resolved.append(f)

    # —— 按（产品, 日期）分组 ——
    keyed: dict[tuple[str, date], list[FileInfo]] = {}
    for f in resolved:
        keyed.setdefault((f.product, f.valuation_date), []).append(f)

    zone_dates_primary: dict[str, set[date]] = {}
    zone_dates_gz: dict[str, set[date]] = {}

    for (product, vdate), members in sorted(
        keyed.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        sorted_members = sorted((_member(m) for m in members), key=_zone_order)
        zones = {m.zone for m in sorted_members}
        hashes = {m.sha256 for m in sorted_members}

        if SourceZone.PRIMARY.value in zones:
            zone_dates_primary.setdefault(product, set()).add(vdate)
        if SourceZone.GZ.value in zones:
            zone_dates_gz.setdefault(product, set()).add(vdate)

        if len(hashes) > 1:
            group = DedupGroup(
                product=product,
                valuation_date=vdate,
                classification=CLASS_SAME_DATE_CONFLICT,
                members=sorted_members,
                keep=None,  # 冲突不自动选择
            )
            result.groups.append(group)
        elif len(sorted_members) > 1:
            group = DedupGroup(
                product=product,
                valuation_date=vdate,
                classification=CLASS_SAME_CONTENT,
                members=sorted_members,
                keep=sorted_members[0].rel_path,
            )
            result.groups.append(group)

    # —— 覆盖差异：primary_only / gz_only（以产品+日期为单位）——
    all_products = sorted(set(zone_dates_primary) | set(zone_dates_gz))
    for product in all_products:
        p_dates = zone_dates_primary.get(product, set())
        g_dates = zone_dates_gz.get(product, set())
        for d in sorted(p_dates - g_dates):
            rep = min(
                (
                    f
                    for f in resolved
                    if f.product == product
                    and f.valuation_date == d
                    and f.zone is SourceZone.PRIMARY
                ),
                key=lambda f: f.rel_path,
            )
            result.primary_only.append(CoverageEntry(product, d, rep.rel_path))
        for d in sorted(g_dates - p_dates):
            rep = min(
                (
                    f
                    for f in resolved
                    if f.product == product
                    and f.valuation_date == d
                    and f.zone is SourceZone.GZ
                ),
                key=lambda f: f.rel_path,
            )
            result.gz_only.append(CoverageEntry(product, d, rep.rel_path))

    # —— 跨区域同名文件对（辅助核对，不作为分组依据）——
    by_name: dict[str, list[FileInfo]] = {}
    for f in resolved:
        by_name.setdefault(f.file_name, []).append(f)
    for file_name in sorted(by_name):
        members = by_name[file_name]
        primaries = [m for m in members if m.zone is SourceZone.PRIMARY]
        gzs = [m for m in members if m.zone is SourceZone.GZ]
        for p in sorted(primaries, key=lambda m: m.rel_path):
            for g in sorted(gzs, key=lambda m: m.rel_path):
                result.same_name_cross_zone.append(
                    SameNamePair(
                        file_name=file_name,
                        primary_rel=p.rel_path,
                        gz_rel=g.rel_path,
                        hash_equal=bool(p.sha256 and p.sha256 == g.sha256),
                        primary_date=p.valuation_date,
                        gz_date=g.valuation_date,
                    )
                )

    # —— 统计 ——
    dup_groups = [g for g in result.groups if g.classification == CLASS_SAME_CONTENT]
    conflict_groups = [
        g for g in result.groups if g.classification == CLASS_SAME_DATE_CONFLICT
    ]
    cross_zone_pairs = [p for p in result.same_name_cross_zone]
    result.stats = {
        "valuation_files": len(valuation),
        "resolved_files": len(resolved),
        "duplicate_group_count": len(dup_groups),
        "duplicate_file_count": sum(len(g.members) for g in dup_groups),
        "conflict_group_count": len(conflict_groups),
        "conflict_file_count": sum(len(g.members) for g in conflict_groups),
        "primary_only_count": len(result.primary_only),
        "gz_only_count": len(result.gz_only),
        "unresolved_count": len(result.unresolved),
        "same_name_pair_count": len(cross_zone_pairs),
        "same_name_hash_equal_count": sum(1 for p in cross_zone_pairs if p.hash_equal),
        "same_name_hash_diff_count": sum(
            1 for p in cross_zone_pairs if not p.hash_equal
        ),
    }
    return result
