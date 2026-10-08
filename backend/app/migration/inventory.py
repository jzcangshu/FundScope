"""Build a migration inventory from the existing valuation inventory tool.

This module deliberately does not parse workbooks or persist valuation data. It
loads the existing scanner and deduplication modules, then turns their result
into migration actions for the official import service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tools.valuation_inventory import dedup, scanner
from tools.valuation_inventory.dedup import DedupGroup, DedupResult
from tools.valuation_inventory.models import FileInfo
from tools.valuation_inventory.scanner import ScanOptions, ScanResult

ACTION_IMPORT = "import"
ACTION_IMPORT_GZ_ONLY = "import_gz_only"
ACTION_SKIP_DUPLICATE = "skip_duplicate"
ACTION_SKIP_NON_VALUATION = "skip_non_valuation"
ACTION_NEEDS_REVIEW = "needs_review"
UPLOAD_ACTIONS = frozenset({ACTION_IMPORT, ACTION_IMPORT_GZ_ONLY})

CLASS_SAME_CONTENT = "same_content_duplicate"
CLASS_SAME_DATE_CONFLICT = "same_date_conflict"


@dataclass(frozen=True, slots=True)
class MigrationCandidate:
    """One relative source file and the action assigned by the inventory rules."""

    rel_path: str
    product: str | None
    valuation_date: date | None
    sha256: str | None
    size_bytes: int
    source_zone: str
    file_type: str
    is_valuation: bool
    action: str
    duplicate_of: str | None = None
    note: str = ""
    error_message: str = ""

    def static_dict(self) -> dict[str, Any]:
        """Return fields that identify an inventory entry, excluding run state."""

        return {
            "rel_path": self.rel_path,
            "product": self.product,
            "valuation_date": (
                self.valuation_date.isoformat() if self.valuation_date else None
            ),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "source_zone": self.source_zone,
            "file_type": self.file_type,
            "is_valuation": self.is_valuation,
            "action": self.action,
            "duplicate_of": self.duplicate_of,
            "note": self.note,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """The scanner output and its existing deduplication result."""

    root_name: str
    files: tuple[FileInfo, ...]
    dedup_result: DedupResult


ScanFunction = Callable[[Path, int], ScanResult]
DedupFunction = Callable[[list[FileInfo]], DedupResult]


def build_inventory(
    root: Path,
    *,
    workers: int = 1,
    scan_fn: ScanFunction | None = None,
    dedup_fn: DedupFunction | None = None,
) -> InventorySnapshot:
    """Scan a source directory with the existing inventory implementation."""

    source_root = root.resolve()
    if not source_root.is_dir():
        raise ValueError("source root does not exist or is not a directory")
    if workers < 1:
        raise ValueError("workers must be at least 1")

    scanner_result = (scan_fn or _scan_with_existing_tool)(source_root, workers)
    files = tuple(sorted(scanner_result.files, key=lambda info: info.rel_path))
    dedup_result = (dedup_fn or _dedup_with_existing_tool)(list(files))
    return InventorySnapshot(
        root_name=source_root.name,
        files=files,
        dedup_result=dedup_result,
    )


def classify_candidates(
    files: list[FileInfo] | tuple[FileInfo, ...], dedup_result: DedupResult
) -> tuple[MigrationCandidate, ...]:
    """Apply the existing product/date/hash and primary-first migration rules."""

    by_rel = {info.rel_path: info for info in files}
    candidates: dict[str, MigrationCandidate] = {}
    grouped_rels: set[str] = set()

    for dedup_group in sorted(dedup_result.groups, key=_dedup_group_sort_key):
        member_rels = sorted(
            member.rel_path
            for member in dedup_group.members
            if member.rel_path in by_rel
        )
        grouped_rels.update(member_rels)
        classification = dedup_group.classification
        if classification == CLASS_SAME_DATE_CONFLICT:
            note = "same_date_conflict: " + ";".join(member_rels)
            for rel_path in member_rels:
                candidates[rel_path] = _candidate(
                    by_rel[rel_path], ACTION_NEEDS_REVIEW, note=note
                )
            continue

        if classification == CLASS_SAME_CONTENT:
            keep = dedup_group.keep
            for rel_path in member_rels:
                if rel_path == keep:
                    info = by_rel[rel_path]
                    zone = enum_or_string_value(info.zone)
                    action = (
                        ACTION_IMPORT if zone == "primary" else ACTION_IMPORT_GZ_ONLY
                    )
                    candidates[rel_path] = _candidate(
                        info,
                        action,
                        note="duplicate group keep (primary-first)",
                    )
                else:
                    candidates[rel_path] = _candidate(
                        by_rel[rel_path],
                        ACTION_SKIP_DUPLICATE,
                        duplicate_of=keep,
                    )
            continue

        for rel_path in member_rels:
            candidates[rel_path] = _candidate(
                by_rel[rel_path],
                ACTION_NEEDS_REVIEW,
                note="unknown dedup classification",
            )

    for info in files:
        rel_path = info.rel_path
        if rel_path in grouped_rels:
            continue
        if not info.is_valuation:
            file_type = enum_or_string_value(info.file_type)
            candidates[rel_path] = _candidate(
                info,
                ACTION_SKIP_NON_VALUATION,
                note=f"not a valuation file: {file_type}",
            )
            continue
        if info.identity_conflict:
            candidates[rel_path] = _candidate(
                info, ACTION_NEEDS_REVIEW, note="identity_conflict"
            )
            continue
        if not info.product:
            candidates[rel_path] = _candidate(
                info, ACTION_NEEDS_REVIEW, note="no_product"
            )
            continue
        if not info.valuation_date:
            candidates[rel_path] = _candidate(info, ACTION_NEEDS_REVIEW, note="no_date")
            continue
        if not info.sha256:
            candidates[rel_path] = _candidate(info, ACTION_NEEDS_REVIEW, note="no_hash")
            continue

        zone = enum_or_string_value(info.zone)
        if zone == "primary":
            action = ACTION_IMPORT
            note = "primary source"
        elif zone == "gz":
            action = ACTION_IMPORT_GZ_ONLY
            note = "gz only date"
        else:
            action = ACTION_NEEDS_REVIEW
            note = f"unexpected zone: {zone}"
        candidates[rel_path] = _candidate(info, action, note=note)

    return tuple(sorted(candidates.values(), key=_candidate_sort_key))


def _candidate(
    info: FileInfo,
    action: str,
    *,
    duplicate_of: str | None = None,
    note: str = "",
) -> MigrationCandidate:
    return MigrationCandidate(
        rel_path=info.rel_path,
        product=info.product,
        valuation_date=info.valuation_date,
        sha256=info.sha256,
        size_bytes=info.size_bytes,
        source_zone=enum_or_string_value(info.zone),
        file_type=enum_or_string_value(info.file_type),
        is_valuation=info.is_valuation,
        action=action,
        duplicate_of=duplicate_of,
        note=note,
        error_message=info.error_message,
    )


def _candidate_sort_key(candidate: MigrationCandidate) -> tuple[str, str, int, str]:
    zone_rank = {"primary": 0, "gz": 1}.get(candidate.source_zone, 2)
    return (
        candidate.product or "",
        candidate.valuation_date.isoformat() if candidate.valuation_date else "",
        zone_rank,
        candidate.rel_path,
    )


def _dedup_group_sort_key(group: DedupGroup) -> tuple[str, str, str]:
    members = sorted(member.rel_path for member in group.members)
    return (
        group.product,
        group.valuation_date.isoformat(),
        members[0] if members else "",
    )


def enum_or_string_value(value: object) -> str:
    """Read an inventory enum value while keeping injected test doubles simple."""

    enum_value = getattr(value, "value", None)
    return str(value if enum_value is None else enum_value)


def _scan_with_existing_tool(root: Path, workers: int) -> ScanResult:
    options = ScanOptions(
        parse_xls=True,
        parse_xlsx=True,
        workers=workers,
    )
    return scanner.scan(root, options)


def _dedup_with_existing_tool(files: list[FileInfo]) -> DedupResult:
    return dedup.analyze(files)
