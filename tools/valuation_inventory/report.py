"""报告输出：inventory.json / inventory.csv / summary.md / dedup-groups.json / migration-candidates.csv。

约定：
- 报告只包含相对 --root 的 POSIX 路径，不写任何绝对路径。
- 全部输出按确定性顺序（默认相对路径、产品、日期）排序，同样输入得到稳定结果。
- CSV 使用 utf-8-sig，便于 Excel 直接打开中文。
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import pairwise
from pathlib import Path

from . import dedup as dd
from .models import FileInfo, ParseStatus, SourceZone

SCHEMA_VERSION = "1"
JSON_INDENT = 2
# 单个缺口内最多列出的缺失工作日日期
MAX_GAP_DATES_SHOWN = 15


@dataclass
class ReportConfig:
    root_name: str
    parse_xls: bool = True
    parse_xlsx: bool = True
    workers: int = 1


@dataclass
class Gap:
    prev_date: date
    next_date: date
    missing_weekdays: list[date]


# ---------------------------------------------------------------------------
# 统计汇总
# ---------------------------------------------------------------------------


def weekday_gaps(sorted_dates: list[date]) -> list[Gap]:
    """相邻两个已有日期之间的工作日（周一至周五）缺口。

    说明：估值表按交易日出具，周六日不视为缺口；法定节假日会表现为缺口，
    需要人工结合日历判断，本工具只做提示不做交易日历推断。
    """
    gaps: list[Gap] = []
    for d1, d2 in pairwise(sorted_dates):
        missing = []
        cur = d1.toordinal() + 1
        while cur < d2.toordinal():
            d = date.fromordinal(cur)
            if d.weekday() < 5:
                missing.append(d)
            cur += 1
        if missing:
            gaps.append(Gap(prev_date=d1, next_date=d2, missing_weekdays=missing))
    return gaps


def build_summary(files: list[FileInfo], result: dd.DedupResult) -> dict:
    """汇总统计：供 inventory.json 的 summary 块与 summary.md 共用。"""
    valuation = [f for f in files if f.is_valuation]

    by_ext: dict[str, int] = {}
    by_zone: dict[str, int] = {}
    for f in files:
        by_ext[f.ext or "(无扩展名)"] = by_ext.get(f.ext or "(无扩展名)", 0) + 1
        by_zone[f.zone.value] = by_zone.get(f.zone.value, 0) + 1

    products: dict[str, dict[str, int]] = {}
    for f in valuation:
        key = f.product if f.product else "(未识别)"
        bucket = products.setdefault(
            key, {"valuation_primary": 0, "valuation_gz": 0, "valuation_other": 0}
        )
        zone_key = f"valuation_{f.zone.value}"
        bucket[zone_key] = bucket.get(zone_key, 0) + 1

    dated = [f for f in valuation if f.valuation_date]
    all_dates = sorted(f.valuation_date for f in dated)

    per_product: dict[str, dict] = {}
    for product in sorted({f.product for f in valuation if f.product}):
        p_files = [f for f in valuation if f.product == product]
        entry: dict = {}
        for zone in (SourceZone.PRIMARY, SourceZone.GZ):
            z_dates = sorted(
                {
                    f.valuation_date
                    for f in p_files
                    if f.zone is zone and f.valuation_date
                }
            )
            entry[f"{zone.value}_count"] = len([f for f in p_files if f.zone is zone])
            entry[f"{zone.value}_date_min"] = (
                z_dates[0].isoformat() if z_dates else None
            )
            entry[f"{zone.value}_date_max"] = (
                z_dates[-1].isoformat() if z_dates else None
            )
            entry[f"{zone.value}_date_count"] = len(z_dates)
        primary_dates = sorted(
            {
                f.valuation_date
                for f in p_files
                if f.zone is SourceZone.PRIMARY and f.valuation_date
            }
        )
        gaps = weekday_gaps(primary_dates)
        entry["primary_gap_count"] = len(gaps)
        entry["primary_gaps"] = [
            {
                "from": g.prev_date.isoformat(),
                "to": g.next_date.isoformat(),
                "missing_weekdays": [d.isoformat() for d in g.missing_weekdays],
            }
            for g in gaps
        ]
        per_product[product] = entry

    no_product = [f for f in valuation if not f.product]
    no_date = [f for f in valuation if not f.valuation_date]
    failures = [f for f in files if f.parse_status is ParseStatus.FAILED]

    return {
        "total_files": len(files),
        "by_extension": dict(sorted(by_ext.items())),
        "by_zone": dict(sorted(by_zone.items())),
        "by_product_valuation": dict(sorted(products.items())),
        "valuation_count": len(valuation),
        "non_valuation_count": len(files) - len(valuation),
        "valuation_without_product_count": len(no_product),
        "valuation_without_date_count": len(no_date),
        "identity_conflict_count": len([f for f in valuation if f.identity_conflict]),
        "read_failure_count": len(failures),
        "valuation_date_min": all_dates[0].isoformat() if all_dates else None,
        "valuation_date_max": all_dates[-1].isoformat() if all_dates else None,
        "per_product": per_product,
        "dedup": dict(result.stats),
    }


# ---------------------------------------------------------------------------
# 输出器
# ---------------------------------------------------------------------------


def write_inventory_json(
    out: Path, files: list[FileInfo], summary: dict, config: ReportConfig
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": config.root_name,
        "config": {
            "parse_xls": config.parse_xls,
            "parse_xlsx": config.parse_xlsx,
            "workers": config.workers,
        },
        "summary": summary,
        "files": [f.as_dict() for f in files],
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT),
        encoding="utf-8",
    )


def write_inventory_csv(out: Path, files: list[FileInfo]) -> None:
    columns = FileInfo.csv_columns()
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for f in files:
            writer.writerow(f.as_csv_row())


def write_dedup_json(out: Path, result: dd.DedupResult) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "groups": [g.as_dict() for g in result.groups],
        "primary_only": [e.as_dict() for e in result.primary_only],
        "gz_only": [e.as_dict() for e in result.gz_only],
        "unresolved_identity": [u.as_dict() for u in result.unresolved],
        "same_name_cross_zone": [p.as_dict() for p in result.same_name_cross_zone],
        "stats": result.stats,
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT),
        encoding="utf-8",
    )


def write_migration_csv(
    out: Path, files: list[FileInfo], result: dd.DedupResult
) -> None:
    """迁移候选清单：只包含估值表；冲突文件逐个列出且不自动选择。

    action 取值：
    - import            主目录文件，产品+日期明确，无冲突 → 可导入
    - import_gz_only    仅 gz 存在的日期 → 补充候选，导入前复核
    - skip_duplicate    与保留文件哈希相同 → 不再导入
    - needs_review      内容冲突 / 身份未识别 / 其他区域 → 人工复核
    """
    by_rel = {f.rel_path: f for f in files}
    rows: list[dict] = []

    def _row(f: FileInfo, action: str, duplicate_of: str = "", note: str = "") -> dict:
        return {
            "product": f.product or "",
            "valuation_date": f.valuation_date.isoformat() if f.valuation_date else "",
            "action": action,
            "source_zone": f.zone.value,
            "rel_path": f.rel_path,
            "sha256": f.sha256 or "",
            "duplicate_of": duplicate_of,
            "note": note,
        }

    # 组内文件：冲突全部复核；重复保留第一个、其余跳过
    grouped_rels: set[str] = set()
    for group in result.groups:
        member_infos = [
            by_rel[m.rel_path] for m in group.members if m.rel_path in by_rel
        ]
        grouped_rels.update(m.rel_path for m in group.members)
        if group.classification == dd.CLASS_SAME_DATE_CONFLICT:
            others = ";".join(sorted(m.rel_path for m in group.members))
            for info in member_infos:
                rows.append(
                    _row(info, "needs_review", note=f"same_date_conflict: {others}")
                )
        else:
            keep_rel = group.keep or ""
            for info in sorted(member_infos, key=lambda x: x.rel_path):
                if info.rel_path == keep_rel:
                    zone = info.zone.value
                    action = (
                        "import"
                        if zone == SourceZone.PRIMARY.value
                        else "import_gz_only"
                    )
                    rows.append(
                        _row(info, action, note="duplicate group keep (primary-first)")
                    )
                else:
                    rows.append(_row(info, "skip_duplicate", duplicate_of=keep_rel))

    # 不在任何组内的已识别估值表（单一文件、产品+日期明确）
    for f in files:
        if not f.is_valuation or f.rel_path in grouped_rels:
            continue
        if f.identity_conflict:
            rows.append(_row(f, "needs_review", note="identity_conflict"))
        elif not f.product:
            rows.append(_row(f, "needs_review", note="no_product"))
        elif not f.valuation_date:
            rows.append(_row(f, "needs_review", note="no_date"))
        elif f.zone is SourceZone.PRIMARY:
            rows.append(_row(f, "import"))
        elif f.zone is SourceZone.GZ:
            rows.append(_row(f, "import_gz_only", note="gz only date"))
        else:
            rows.append(
                _row(f, "needs_review", note=f"unexpected zone: {f.zone.value}")
            )

    def _sort_key(r: dict) -> tuple:
        zone_rank = {"primary": 0, "gz": 1}.get(r["source_zone"], 2)
        return (r["product"], r["valuation_date"], zone_rank, r["rel_path"])

    rows.sort(key=_sort_key)
    columns = [
        "product",
        "valuation_date",
        "action",
        "source_zone",
        "rel_path",
        "sha256",
        "duplicate_of",
        "note",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _md_table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return lines


def write_summary_md(
    out: Path,
    files: list[FileInfo],
    result: dd.DedupResult,
    summary: dict,
    config: ReportConfig,
) -> None:
    s = summary
    lines: list[str] = []
    lines.append("# 历史估值文件盘点汇总")
    lines.append("")
    lines.append(f"- 扫描根目录：`{config.root_name}`（报告内路径均相对该目录）")
    lines.append(f"- 文件总数：{s['total_files']}")
    lines.append(
        f"- 估值表数量：{s['valuation_count']}；非估值表数量：{s['non_valuation_count']}"
    )
    if s["valuation_date_min"]:
        lines.append(
            f"- 估值日期范围：{s['valuation_date_min']} 至 {s['valuation_date_max']}"
        )
    lines.append("")

    lines.append("## 按扩展名统计")
    lines += _md_table(
        ["扩展名", "文件数"], [[k, v] for k, v in s["by_extension"].items()]
    )
    lines.append("")

    lines.append("## 按来源区域统计")
    lines += _md_table(["区域", "文件数"], [[k, v] for k, v in s["by_zone"].items()])
    lines.append("")

    lines.append("## 按产品统计（估值表）")
    lines += _md_table(
        ["产品", "主目录", "gz", "其他区域"],
        [
            [
                p,
                c.get("valuation_primary", 0),
                c.get("valuation_gz", 0),
                c.get("valuation_other", 0),
            ]
            for p, c in s["by_product_valuation"].items()
        ],
    )
    lines.append("")

    lines.append("## 去重与冲突")
    d = s["dedup"]
    lines += _md_table(
        ["指标", "数量"],
        [
            ["重复组（同产品同日期同哈希）", d.get("duplicate_group_count", 0)],
            ["重复文件数（组内全部成员）", d.get("duplicate_file_count", 0)],
            ["内容冲突组（同产品同日期不同哈希）", d.get("conflict_group_count", 0)],
            ["内容冲突文件数", d.get("conflict_file_count", 0)],
            ["主目录独有（产品+日期）", d.get("primary_only_count", 0)],
            ["gz 独有（产品+日期）", d.get("gz_only_count", 0)],
            [
                "身份未识别文件（产品、日期或哈希缺失/冲突）",
                d.get("unresolved_count", 0),
            ],
        ],
    )
    lines.append("")

    lines.append("## 数据质量问题计数")
    lines += _md_table(
        ["问题", "数量"],
        [
            ["无法识别产品的估值表", s["valuation_without_product_count"]],
            ["无法识别日期的估值表", s["valuation_without_date_count"]],
            ["产品识别冲突（identity_conflict）", s["identity_conflict_count"]],
            ["读取/解析失败文件", s["read_failure_count"]],
        ],
    )
    lines.append("")

    lines.append("## 每个产品的日期覆盖与缺口")
    lines.append("")
    lines.append(
        "缺口口径：产品主目录内相邻两个已有估值日期之间缺失的工作日（周一至周五）。"
        "法定节假日会表现为缺口，需要人工结合交易日历判断。"
    )
    lines.append("")
    for product, entry in s["per_product"].items():
        lines.append(f"### {product}")
        lines += _md_table(
            ["区域", "估值表数", "唯一日期数", "最早日期", "最晚日期"],
            [
                [
                    "主目录",
                    entry["primary_count"],
                    entry["primary_date_count"],
                    entry["primary_date_min"],
                    entry["primary_date_max"],
                ],
                [
                    "gz",
                    entry["gz_count"],
                    entry["gz_date_count"],
                    entry["gz_date_min"],
                    entry["gz_date_max"],
                ],
            ],
        )
        gaps = entry["primary_gaps"]
        if not gaps:
            lines.append("- 主目录日期无工作日缺口。")
        else:
            lines.append(f"- 主目录工作日缺口：{entry['primary_gap_count']} 处")
            gap_rows = []
            for g in gaps:
                shown = [d for d in g["missing_weekdays"][:MAX_GAP_DATES_SHOWN]]
                suffix = (
                    " …" if len(g["missing_weekdays"]) > MAX_GAP_DATES_SHOWN else ""
                )
                gap_rows.append(
                    [
                        f"{g['from']} → {g['to']}",
                        len(g["missing_weekdays"]),
                        ", ".join(shown) + suffix,
                    ]
                )
            lines += _md_table(["缺口区间", "缺失工作日数", "缺失日期"], gap_rows)
        lines.append("")

    lines.append("## 需要人工复核")
    lines.append("")

    conflict_groups = [
        g for g in result.groups if g.classification == dd.CLASS_SAME_DATE_CONFLICT
    ]
    lines.append(f"### 同产品同日期内容冲突（{len(conflict_groups)} 组，不自动选择）")
    if conflict_groups:
        rows = []
        for g in conflict_groups:
            members = "; ".join(
                f"{m.rel_path} ({m.zone}, sha256={m.sha256[:12] if m.sha256 else '?'})"
                for m in g.members
            )
            rows.append([g.product, g.valuation_date.isoformat(), members])
        lines += _md_table(["产品", "估值日期", "冲突文件"], rows)
    else:
        lines.append("- 无。")
    lines.append("")

    lines.append(f"### 身份未识别（{len(result.unresolved)} 个文件）")
    if result.unresolved:
        lines += _md_table(
            ["文件", "原因", "产品", "估值日期"],
            [
                [u.rel_path, u.reason, u.product or "", u.valuation_date or ""]
                for u in result.unresolved
            ],
        )
    else:
        lines.append("- 无。")
    lines.append("")

    failures = [f for f in files if f.parse_status is ParseStatus.FAILED]
    lines.append(f"### 读取/解析失败（{len(failures)} 个文件）")
    if failures:
        lines += _md_table(
            ["文件", "错误类型", "错误信息"],
            [[f.rel_path, f.error_type.value, f.error_message] for f in failures],
        )
    else:
        lines.append("- 无。")
    lines.append("")

    same_name_diff = [p for p in result.same_name_cross_zone if not p.hash_equal]
    lines.append(f"### 跨区域同名但哈希不同（{len(same_name_diff)} 对）")
    if same_name_diff:
        lines += _md_table(
            ["文件名", "主目录文件", "主目录表内日期", "gz 文件", "gz 表内日期"],
            [
                [
                    p.file_name,
                    p.primary_rel,
                    p.primary_date or "",
                    p.gz_rel,
                    p.gz_date or "",
                ]
                for p in same_name_diff
            ],
        )
    else:
        lines.append("- 无。")
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")


def write_reports(
    out_dir: Path,
    files: list[FileInfo],
    result: dd.DedupResult,
    config: ReportConfig,
    fmt: str = "all",
) -> list[str]:
    """按 fmt（json/csv/markdown/all）写出报告，返回生成的文件名列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = build_summary(files, result)
    written: list[str] = []
    want_json = fmt in ("json", "all")
    want_csv = fmt in ("csv", "all")
    want_md = fmt in ("markdown", "all")
    if want_json:
        write_inventory_json(out_dir / "inventory.json", files, summary, config)
        write_dedup_json(out_dir / "dedup-groups.json", result)
        written += ["inventory.json", "dedup-groups.json"]
    if want_csv:
        write_inventory_csv(out_dir / "inventory.csv", files)
        write_migration_csv(out_dir / "migration-candidates.csv", files, result)
        written += ["inventory.csv", "migration-candidates.csv"]
    if want_md:
        write_summary_md(out_dir / "summary.md", files, result, summary, config)
        written += ["summary.md"]
    return written
