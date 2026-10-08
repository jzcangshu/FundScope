"""Deterministic migration report output with relative source paths only."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .manifest import MigrationManifest


def build_report(manifest: MigrationManifest, inventory: Any) -> dict[str, Any]:
    entries = [entry.as_dict() for entry in manifest.entries]
    action_counts = Counter(entry.action for entry in manifest.entries)
    status_counts = Counter(entry.status for entry in manifest.entries)
    summary = {
        "scanned_file_count": len(inventory.files),
        "valuation_file_count": sum(
            1 for info in inventory.files if getattr(info, "is_valuation", False)
        ),
        "candidate_count": action_counts.get("import", 0),
        "gz_candidate_count": action_counts.get("import_gz_only", 0),
        "needs_review_count": action_counts.get("needs_review", 0),
        "duplicate_skipped_count": action_counts.get("skip_duplicate", 0),
        "non_valuation_skipped_count": action_counts.get("skip_non_valuation", 0),
        "uploaded_count": status_counts.get("uploaded", 0),
        "failed_count": status_counts.get("failed", 0),
        "pending_count": status_counts.get("pending", 0),
        "batch_status": manifest.batch_status,
    }
    return {
        "schema_version": 1,
        "root_name": manifest.root_name,
        "inventory_fingerprint": manifest.inventory_fingerprint,
        "batch_id": manifest.batch_id,
        "batch_status": manifest.batch_status,
        "last_error": manifest.last_error,
        "summary": summary,
        "entries": entries,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".md", ".markdown"}:
        path.write_text(_markdown(report), encoding="utf-8")
        return
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 历史估值迁移报告",
        "",
        f"- 扫描根目录名称：`{report['root_name']}`（源文件路径均为相对路径）",
        f"- 清单指纹：`{report['inventory_fingerprint']}`",
        f"- 批次状态：`{report['batch_status']}`",
        "",
        "## 汇总",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
    ]
    for key, value in summary.items():
        lines.append(f"| {key} | {value} |")
    if report.get("last_error"):
        lines.extend(("", f"- 最近批次错误：{report['last_error']}"))
    lines.extend(
        (
            "",
            "## 文件明细",
            "",
            "| 动作 | 状态 | 产品 | 估值日期 | 相对路径 | 错误 |",
            "| --- | --- | --- | --- | --- | --- |",
        )
    )
    for entry in report["entries"]:
        values = [
            entry["action"],
            entry["status"],
            entry.get("product") or "",
            entry.get("valuation_date") or "",
            entry["rel_path"],
            entry.get("last_error") or entry.get("error_message") or "",
        ]
        lines.append("| " + " | ".join(_escape(str(value)) for value in values) + " |")
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
