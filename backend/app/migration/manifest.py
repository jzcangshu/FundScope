"""Persistent, relative-path migration manifests and resumable entry state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .inventory import (
    ACTION_NEEDS_REVIEW,
    UPLOAD_ACTIONS,
    InventorySnapshot,
    MigrationCandidate,
    classify_candidates,
)

MANIFEST_SCHEMA_VERSION = 1
STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NEEDS_REVIEW = "needs_review"


class ManifestMismatch(ValueError):
    """Raised when a resume manifest no longer matches the fresh inventory."""


@dataclass(slots=True)
class ManifestEntry:
    rel_path: str
    product: str | None
    valuation_date: date | None
    sha256: str | None
    size_bytes: int
    source_zone: str
    file_type: str
    is_valuation: bool
    action: str
    duplicate_of: str | None
    note: str
    error_message: str
    status: str
    attempts: int = 0
    remote_source_file_id: int | str | None = None
    last_error: str = ""

    @classmethod
    def from_candidate(cls, candidate: MigrationCandidate) -> ManifestEntry:
        validate_relative_path(candidate.rel_path)
        return cls(
            rel_path=candidate.rel_path,
            product=candidate.product,
            valuation_date=candidate.valuation_date,
            sha256=candidate.sha256,
            size_bytes=candidate.size_bytes,
            source_zone=candidate.source_zone,
            file_type=candidate.file_type,
            is_valuation=candidate.is_valuation,
            action=candidate.action,
            duplicate_of=candidate.duplicate_of,
            note=candidate.note,
            error_message=candidate.error_message,
            status=_initial_status(candidate.action),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ManifestEntry:
        rel_path = str(payload["rel_path"])
        validate_relative_path(rel_path)
        raw_date = payload.get("valuation_date")
        return cls(
            rel_path=rel_path,
            product=payload.get("product"),
            valuation_date=date.fromisoformat(raw_date) if raw_date else None,
            sha256=payload.get("sha256"),
            size_bytes=int(payload.get("size_bytes", -1)),
            source_zone=str(payload.get("source_zone", "other")),
            file_type=str(payload.get("file_type", "unknown")),
            is_valuation=bool(payload.get("is_valuation", False)),
            action=str(payload["action"]),
            duplicate_of=payload.get("duplicate_of"),
            note=str(payload.get("note", "")),
            error_message=str(payload.get("error_message", "")),
            status=str(payload.get("status", _initial_status(str(payload["action"])))),
            attempts=int(payload.get("attempts", 0)),
            remote_source_file_id=payload.get("remote_source_file_id"),
            last_error=str(payload.get("last_error", "")),
        )

    def static_dict(self) -> dict[str, Any]:
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

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.static_dict(),
            "status": self.status,
            "attempts": self.attempts,
            "remote_source_file_id": self.remote_source_file_id,
            "last_error": self.last_error,
        }


@dataclass
class MigrationManifest:
    root_name: str
    inventory_fingerprint: str
    entries: list[ManifestEntry]
    batch_id: int | str | None = None
    batch_status: str = "not_started"
    last_error: str = ""

    @classmethod
    def from_inventory(cls, inventory: InventorySnapshot) -> MigrationManifest:
        candidates = classify_candidates(inventory.files, inventory.dedup_result)
        entries = [ManifestEntry.from_candidate(candidate) for candidate in candidates]
        return cls(
            root_name=inventory.root_name,
            inventory_fingerprint=fingerprint(inventory.root_name, entries),
            entries=entries,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MigrationManifest:
        if int(payload.get("schema_version", 0)) != MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported migration manifest schema")
        root_name = str(payload.get("root_name", ""))
        if not root_name or "/" in root_name or "\\" in root_name:
            raise ValueError("manifest root_name must be a name")
        entries = [ManifestEntry.from_dict(item) for item in payload.get("entries", [])]
        paths = [entry.rel_path for entry in entries]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest contains duplicate relative paths")
        return cls(
            root_name=root_name,
            inventory_fingerprint=str(payload["inventory_fingerprint"]),
            entries=entries,
            batch_id=payload.get("batch_id"),
            batch_status=str(payload.get("batch_status", "not_started")),
            last_error=str(payload.get("last_error", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "root_name": self.root_name,
            "inventory_fingerprint": self.inventory_fingerprint,
            "batch_id": self.batch_id,
            "batch_status": self.batch_status,
            "last_error": self.last_error,
            "entries": [entry.as_dict() for entry in self.entries],
        }

    def entry(self, rel_path_or_name: str) -> ManifestEntry:
        exact = next(
            (item for item in self.entries if item.rel_path == rel_path_or_name), None
        )
        if exact is not None:
            return exact
        matches = [
            item
            for item in self.entries
            if Path(item.rel_path).name == rel_path_or_name
        ]
        if len(matches) == 1:
            return matches[0]
        raise KeyError(rel_path_or_name)


def load_manifest(path: Path) -> MigrationManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("migration manifest must be an object")
    return MigrationManifest.from_dict(payload)


def write_manifest(path: Path, manifest: MigrationManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def reconcile_manifest(
    inventory: InventorySnapshot, existing: MigrationManifest | None
) -> MigrationManifest:
    current = MigrationManifest.from_inventory(inventory)
    if existing is None:
        return current
    if (
        existing.root_name != current.root_name
        or existing.inventory_fingerprint != current.inventory_fingerprint
    ):
        raise ManifestMismatch(
            "source inventory changed; start a new manifest or rescan before retry"
        )

    old_by_path = {entry.rel_path: entry for entry in existing.entries}
    if set(old_by_path) != {entry.rel_path for entry in current.entries}:
        raise ManifestMismatch("source inventory entries changed")
    current.entries = [
        replace(
            entry,
            status=old_by_path[entry.rel_path].status,
            attempts=old_by_path[entry.rel_path].attempts,
            remote_source_file_id=old_by_path[entry.rel_path].remote_source_file_id,
            last_error=old_by_path[entry.rel_path].last_error,
        )
        for entry in current.entries
    ]
    current.batch_id = existing.batch_id
    current.batch_status = existing.batch_status
    current.last_error = existing.last_error
    return current


def fingerprint(root_name: str, entries: list[ManifestEntry]) -> str:
    payload = {
        "root_name": root_name,
        "entries": [
            entry.static_dict() for entry in sorted(entries, key=lambda e: e.rel_path)
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_relative_path(rel_path: str) -> None:
    """Reject absolute, traversal, and Windows path forms in persisted manifests."""

    if not rel_path or "\\" in rel_path:
        raise ValueError("manifest paths must be relative POSIX paths")
    if PurePosixPath(rel_path).is_absolute() or PureWindowsPath(rel_path).is_absolute():
        raise ValueError("manifest paths must be relative POSIX paths")
    if ".." in PurePosixPath(rel_path).parts:
        raise ValueError("manifest paths must stay relative")


def _initial_status(action: str) -> str:
    if action in UPLOAD_ACTIONS:
        return STATUS_PENDING
    if action == ACTION_NEEDS_REVIEW:
        return STATUS_NEEDS_REVIEW
    return STATUS_SKIPPED
