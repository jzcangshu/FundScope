"""命令行入口：

    python -m tools.valuation_inventory --root <目录> --out <输出目录> [--format all] [--workers 1] [--parse-xls]

工具只读扫描 --root，报告写入 --out，绝不修改源目录。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, dedup, report, scanner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.valuation_inventory",
        description="历史估值文件只读盘点工具：扫描、哈希、估值表识别、主目录/gz 去重比较与报告输出。",
    )
    parser.add_argument(
        "--root", required=True, help="要扫描的根目录（只读，不会被修改）"
    )
    parser.add_argument("--out", required=True, help="报告输出目录")
    parser.add_argument(
        "--format",
        choices=("json", "csv", "markdown", "all"),
        default="all",
        help="报告格式，默认 all",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发扫描线程数，默认 1（串行）",
    )
    parser.add_argument(
        "--parse-xls",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否读取 .xls 内容（估值日期、核心字段），默认开启；--no-parse-xls 关闭",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"错误：扫描根目录不存在或不是目录：{args.root}", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("错误：--workers 必须 >= 1", file=sys.stderr)
        return 2

    root = root.resolve()
    out_dir = Path(args.out).resolve()
    if out_dir == root or out_dir.is_relative_to(root):
        print("错误：报告输出目录不能位于扫描根目录内", file=sys.stderr)
        return 2
    options = scanner.ScanOptions(
        parse_xls=args.parse_xls,
        parse_xlsx=True,
        workers=args.workers,
    )

    scan_result = scanner.scan(root, options)
    files = scan_result.files
    dedup_result = dedup.analyze(files)
    config = report.ReportConfig(
        root_name=scan_result.root_name,
        parse_xls=options.parse_xls,
        parse_xlsx=options.parse_xlsx,
        workers=options.workers,
    )
    written = report.write_reports(
        out_dir, files, dedup_result, config, fmt=args.format
    )

    valuation = [f for f in files if f.is_valuation]
    failed = [f for f in files if f.parse_status.value == "failed"]
    print(
        f"扫描完成：文件 {len(files)} 个，估值表 {len(valuation)} 个，解析失败 {len(failed)} 个"
    )
    print(
        "去重：重复组 {dup}（{dupf} 文件），冲突组 {conf}（{conff} 文件），"
        "主目录独有 {po}，gz 独有 {go}，身份未识别 {unr}".format(
            dup=dedup_result.stats.get("duplicate_group_count", 0),
            dupf=dedup_result.stats.get("duplicate_file_count", 0),
            conf=dedup_result.stats.get("conflict_group_count", 0),
            conff=dedup_result.stats.get("conflict_file_count", 0),
            po=dedup_result.stats.get("primary_only_count", 0),
            go=dedup_result.stats.get("gz_only_count", 0),
            unr=dedup_result.stats.get("unresolved_count", 0),
        )
    )
    print(f"报告已写入：{out_dir}")
    for name in sorted(written):
        print(f"  - {name}")
    return 0
