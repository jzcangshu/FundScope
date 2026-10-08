"""Read-only migration orchestration, dry-run, checkpointing, and retry."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .inventory import (
    UPLOAD_ACTIONS,
    DedupFunction,
    ScanFunction,
    build_inventory,
)
from .manifest import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_UPLOADED,
    MigrationManifest,
    load_manifest,
    reconcile_manifest,
    validate_relative_path,
    write_manifest,
)
from .report import build_report, write_report
from .transport import ImportTransport


@dataclass(frozen=True, slots=True)
class MigrationRunResult:
    manifest: MigrationManifest
    report: dict[str, object]

    @property
    def ok(self) -> bool:
        return not any(
            entry.action in UPLOAD_ACTIONS
            and entry.status in {STATUS_FAILED, STATUS_PENDING}
            for entry in self.manifest.entries
        ) and self.manifest.batch_status not in {"failed", "open"}


def run_migration(
    source_root: Path,
    manifest_path: Path,
    report_path: Path,
    *,
    transport: ImportTransport | None = None,
    dry_run: bool = False,
    workers: int = 1,
    scan_fn: ScanFunction | None = None,
    dedup_fn: DedupFunction | None = None,
) -> MigrationRunResult:
    """Build or resume a manifest, then optionally upload eligible candidates."""

    root = source_root.resolve()
    _ensure_external_output(root, manifest_path)
    _ensure_external_output(root, report_path)
    inventory = build_inventory(
        root,
        workers=workers,
        scan_fn=scan_fn,
        dedup_fn=dedup_fn,
    )
    existing = load_manifest(manifest_path) if manifest_path.exists() else None
    manifest = reconcile_manifest(inventory, existing)
    write_manifest(manifest_path, manifest)

    if not dry_run:
        if transport is None:
            raise ValueError("transport is required unless dry_run is enabled")
        upload_manifest(
            root,
            manifest,
            transport,
            save=lambda: write_manifest(manifest_path, manifest),
        )
        write_manifest(manifest_path, manifest)

    report = build_report(manifest, inventory)
    write_report(report_path, report)
    return MigrationRunResult(manifest=manifest, report=report)


def upload_manifest(
    source_root: Path,
    manifest: MigrationManifest,
    transport: ImportTransport,
    *,
    save: Callable[[], None] | None = None,
) -> None:
    """Upload eligible entries and checkpoint every individual attempt."""

    pending = [
        entry
        for entry in manifest.entries
        if entry.action in UPLOAD_ACTIONS
        and entry.status in {STATUS_PENDING, STATUS_FAILED}
    ]
    if not pending:
        if manifest.batch_id is not None and manifest.batch_status != "completed":
            _complete_batch(manifest, transport, save)
        return

    if manifest.batch_id is None:
        try:
            manifest.batch_id = transport.create_batch("migration")
            manifest.batch_status = "created"
            manifest.last_error = ""
            _checkpoint(save)
        except Exception as exc:  # noqa: BLE001 - transport failures are retryable
            manifest.batch_status = "failed"
            manifest.last_error = _redact_error(str(exc), source_root)
            _checkpoint(save)
            return

    root = source_root.resolve()
    for entry in pending:
        entry.attempts += 1
        _checkpoint(save)
        try:
            path = _resolve_source_path(root, entry.rel_path)
            _verify_source(path, entry.sha256, entry.size_bytes)
            with path.open("rb") as stream:
                receipt = transport.upload_file(
                    manifest.batch_id,
                    Path(entry.rel_path).name,
                    stream,
                    entry.size_bytes,
                )
        except Exception as exc:  # noqa: BLE001 - one file must not stop the batch
            entry.status = STATUS_FAILED
            entry.last_error = _redact_error(str(exc) or type(exc).__name__, root)
            manifest.batch_status = "open"
            _checkpoint(save)
            continue
        entry.status = STATUS_UPLOADED
        entry.remote_source_file_id = receipt.remote_source_file_id
        manifest.batch_status = "open"
        _checkpoint(save)

    if any(
        entry.action in UPLOAD_ACTIONS and entry.status != STATUS_UPLOADED
        for entry in manifest.entries
    ):
        manifest.batch_status = "open"
        _checkpoint(save)
        return
    _complete_batch(manifest, transport, save)


def _complete_batch(
    manifest: MigrationManifest,
    transport: ImportTransport,
    save: Callable[[], None] | None,
) -> None:
    if manifest.batch_id is None or manifest.batch_status == "completed":
        return
    try:
        transport.complete_batch(manifest.batch_id)
    except Exception as exc:  # noqa: BLE001 - completion can be retried safely
        manifest.batch_status = "failed"
        manifest.last_error = str(exc) or type(exc).__name__
        _checkpoint(save)
        return
    manifest.batch_status = "completed"
    _checkpoint(save)


def _resolve_source_path(root: Path, rel_path: str) -> Path:
    validate_relative_path(rel_path)
    candidate = root / Path(rel_path)
    if candidate.is_symlink():
        raise ValueError("source path is a link")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("source path escapes root")
    if not resolved.is_file():
        raise FileNotFoundError("source file is missing")
    return resolved


def _verify_source(path: Path, expected_hash: str | None, expected_size: int) -> None:
    if expected_hash is None:
        raise ValueError("source hash is missing")
    if path.stat().st_size != expected_size:
        raise ValueError("source size changed")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise ValueError("source hash changed")


def _ensure_external_output(root: Path, output: Path) -> None:
    resolved = output.resolve(strict=False)
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError("manifest and report must be outside the source root")


def _checkpoint(save: Callable[[], None] | None) -> None:
    if save is not None:
        save()


def _redact_error(text: str, root: Path) -> str:
    result = text
    values = {str(root), str(root.resolve())}
    values.update(value.replace("\\", "/") for value in tuple(values))
    for value in sorted(values, key=len, reverse=True):
        result = result.replace(value, "<root>")
    return result[:500]
