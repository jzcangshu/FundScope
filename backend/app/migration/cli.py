"""Command line entry point for the local historical migration tool."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import cast

from .runner import run_migration
from .transport import HttpImportTransport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.migration",
        description="Scan and resume historical valuation migration through the import API.",
    )
    parser.add_argument("--root", required=True, help="read-only source directory")
    parser.add_argument("--manifest", required=True, help="manifest JSON path")
    parser.add_argument("--report", required=True, help="migration report path")
    parser.add_argument("--base-url", help="backend base URL, required for upload")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scan and write the manifest/report without creating an import batch",
    )
    parser.add_argument("--workers", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print("错误：源目录不存在或不是目录")
        return 2
    if args.workers < 1:
        print("错误：workers 必须至少为 1")
        return 2
    if not args.dry_run and not args.base_url:
        print("错误：上传模式需要 base URL")
        return 2

    transport = None
    if not args.dry_run:
        session_token = os.getenv("MIGRATION_TOKEN")
        if not session_token:
            print("错误：上传模式需要 MIGRATION_TOKEN（登录会话值）")
            return 2
        transport = HttpImportTransport(
            args.base_url,
            token=session_token,
        )
    try:
        result = run_migration(
            root,
            Path(args.manifest),
            Path(args.report),
            transport=transport,
            dry_run=args.dry_run,
            workers=args.workers,
        )
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2

    summary = cast(dict[str, object], result.report["summary"])
    print(
        "迁移完成：扫描 {scanned} 个文件，候选 {candidate} 个，上传 {uploaded} 个，"
        "失败 {failed} 个，需复核 {review} 个，批次状态 {batch}".format(
            scanned=summary["scanned_file_count"],
            candidate=summary["candidate_count"],
            uploaded=summary["uploaded_count"],
            failed=summary["failed_count"],
            review=summary["needs_review_count"],
            batch=summary["batch_status"],
        )
    )
    return 0 if result.ok or args.dry_run else 1
