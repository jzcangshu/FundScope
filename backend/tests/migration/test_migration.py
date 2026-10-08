from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from tools.valuation_inventory.dedup import DedupGroup, DedupResult, GroupMember
from tools.valuation_inventory.scanner import ScanResult

from app.auth.dependencies import SESSION_COOKIE_NAME
from app.migration.cli import main
from app.migration.inventory import build_inventory, classify_candidates
from app.migration.manifest import MigrationManifest
from app.migration.runner import run_migration
from app.migration.transport import HttpImportTransport, HttpResponse, UploadReceipt


@dataclass(frozen=True)
class FakeFile:
    rel_path: str
    source_zone: str = "primary"
    product: str | None = "梦溪一号"
    valuation_date: date | None = date(2026, 1, 5)
    sha256: str | None = "a" * 64
    size_bytes: int = 10
    file_type: str = "valuation_xls"
    is_valuation: bool = True
    identity_conflict: bool = False
    parse_status: str = "ok"
    error_message: str = ""

    @property
    def zone(self) -> str:
        return self.source_zone

    @property
    def ext(self) -> str:
        return Path(self.rel_path).suffix

    @property
    def file_name(self) -> str:
        return Path(self.rel_path).name


def member(rel_path: str) -> GroupMember:
    return GroupMember(
        rel_path=rel_path,
        zone="primary",
        file_name=Path(rel_path).name,
        sha256=None,
        valuation_date=date(2026, 1, 5),
    )


def group(classification: str, *rel_paths: str, keep: str | None = None) -> DedupGroup:
    return DedupGroup(
        product="梦溪一号",
        valuation_date=date(2026, 1, 5),
        classification=classification,
        members=[member(rel_path) for rel_path in rel_paths],
        keep=keep,
    )


def fake_dedup(*groups: DedupGroup) -> DedupResult:
    return DedupResult(groups=list(groups))


def test_inventory_adapter_uses_injected_scan_and_dedup(tmp_path: Path) -> None:
    root = tmp_path / "估值表A"
    root.mkdir()
    files = (FakeFile("梦溪一号估值表/a.xls"),)
    calls: list[tuple[Path, int]] = []

    def scan(source_root: Path, workers: int) -> ScanResult:
        calls.append((source_root, workers))
        return ScanResult(files=list(files), root_name=source_root.name)

    result = build_inventory(
        root,
        workers=4,
        scan_fn=scan,
        dedup_fn=lambda scanned: fake_dedup(),
    )

    assert result.root_name == "估值表A"
    assert result.files == files
    assert calls == [(root.resolve(), 4)]


def test_classification_keeps_primary_skips_same_hash_and_reviews_conflict() -> None:
    primary = FakeFile("梦溪一号估值表/2026/primary.xls", sha256="a" * 64)
    gz_duplicate = FakeFile(
        "gz/梦溪一号估值表/2026/copy.xls", source_zone="gz", sha256="a" * 64
    )
    conflict_primary = FakeFile(
        "梦溪一号估值表/2026/conflict.xls",
        sha256="b" * 64,
        valuation_date=date(2026, 1, 6),
    )
    conflict_gz = FakeFile(
        "gz/梦溪一号估值表/2026/conflict.xls",
        source_zone="gz",
        sha256="c" * 64,
        valuation_date=date(2026, 1, 6),
    )
    transaction = FakeFile(
        "千帆一号估值表/交易记录备份/交易记录.xlsx",
        product="千帆一号",
        valuation_date=None,
        sha256="d" * 64,
        file_type="transaction_xlsx",
        is_valuation=False,
    )
    sidecar = FakeFile(
        "天衡上将估值表/交易记录.xlsx.bak_cum_nav",
        product="天衡上将",
        valuation_date=None,
        sha256="e" * 64,
        file_type="backup_sidecar",
        is_valuation=False,
    )
    files = [primary, gz_duplicate, conflict_primary, conflict_gz, transaction, sidecar]
    dedup_result = fake_dedup(
        group(
            "same_content_duplicate",
            primary.rel_path,
            gz_duplicate.rel_path,
            keep=primary.rel_path,
        ),
        group("same_date_conflict", conflict_primary.rel_path, conflict_gz.rel_path),
    )

    entries = classify_candidates(files, dedup_result)
    by_path = {entry.rel_path: entry for entry in entries}

    assert by_path[primary.rel_path].action == "import"
    assert by_path[gz_duplicate.rel_path].action == "skip_duplicate"
    assert by_path[gz_duplicate.rel_path].duplicate_of == primary.rel_path
    assert by_path[conflict_primary.rel_path].action == "needs_review"
    assert by_path[conflict_gz.rel_path].action == "needs_review"
    assert by_path[transaction.rel_path].action == "skip_non_valuation"
    assert by_path[sidecar.rel_path].action == "skip_non_valuation"
    assert entries == classify_candidates(list(reversed(files)), dedup_result)


class FakeTransport:
    def __init__(self, fail_once: set[str] | None = None) -> None:
        self.fail_once = set(fail_once or ())
        self.create_calls = 0
        self.upload_calls: list[str] = []
        self.complete_calls: list[int] = []

    def create_batch(self, source_type: str) -> int:
        assert source_type == "migration"
        self.create_calls += 1
        return 41

    def upload_file(
        self,
        batch_id: int,
        original_filename: str,
        stream,
        file_size: int,
    ) -> UploadReceipt:
        assert batch_id == 41
        assert Path(original_filename).name == original_filename
        assert len(stream.read()) == file_size
        self.upload_calls.append(original_filename)
        if original_filename in self.fail_once:
            self.fail_once.remove(original_filename)
            raise RuntimeError("temporary failure for F:\\AgentWorks\\估值表A")
        return UploadReceipt(
            remote_source_file_id=len(self.upload_calls), duplicate=False
        )

    def complete_batch(self, batch_id: int) -> None:
        self.complete_calls.append(batch_id)


def test_resume_retries_failed_file_without_touching_source_and_redacts_report(
    tmp_path: Path,
) -> None:
    root = tmp_path / "估值表A"
    first_path = root / "梦溪一号估值表" / "2026" / "first.xls"
    second_path = root / "梦溪一号估值表" / "2026" / "second.xls"
    first_path.parent.mkdir(parents=True)
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first = FakeFile(
        "梦溪一号估值表/2026/first.xls",
        sha256=hashlib.sha256(b"first").hexdigest(),
        size_bytes=5,
    )
    second = FakeFile(
        "梦溪一号估值表/2026/second.xls",
        sha256=hashlib.sha256(b"second").hexdigest(),
        size_bytes=6,
        valuation_date=date(2026, 1, 6),
    )
    snapshot_before = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }

    def scan(source_root: Path, workers: int) -> ScanResult:
        return ScanResult(files=[first, second], root_name=source_root.name)

    out = tmp_path / "reports"
    manifest_path = out / "migration-manifest.json"
    report_path = out / "migration-report.json"
    transport = FakeTransport({"second.xls"})
    first_run = run_migration(
        root,
        manifest_path,
        report_path,
        transport=transport,
        scan_fn=scan,
        dedup_fn=lambda scanned: fake_dedup(),
    )

    assert first_run.manifest.batch_id == 41
    assert first_run.manifest.entry("first.xls").status == "uploaded"
    assert first_run.manifest.entry("second.xls").status == "failed"
    assert transport.complete_calls == []

    transport.fail_once.clear()
    second_run = run_migration(
        root,
        manifest_path,
        report_path,
        transport=transport,
        scan_fn=scan,
        dedup_fn=lambda scanned: fake_dedup(),
    )

    assert second_run.manifest.batch_id == 41
    assert second_run.manifest.entry("second.xls").status == "uploaded"
    assert transport.create_calls == 1
    assert transport.upload_calls == ["first.xls", "second.xls", "second.xls"]
    assert transport.complete_calls == [41]

    snapshot_after = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }
    assert snapshot_after == snapshot_before

    report_text = report_path.read_text(encoding="utf-8")
    assert str(root) not in report_text
    assert "梦溪一号估值表/2026/first.xls" in report_text
    assert "temporary failure" in report_text
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert str(root) not in manifest_text


def test_dry_run_writes_manifest_and_never_calls_transport(tmp_path: Path) -> None:
    root = tmp_path / "估值表A"
    source = root / "梦溪一号估值表" / "dry-run.xls"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"dry-run")
    file_info = FakeFile(
        "梦溪一号估值表/dry-run.xls",
        sha256=hashlib.sha256(b"dry-run").hexdigest(),
        size_bytes=7,
    )

    class ExplodingTransport:
        def create_batch(self, source_type: str) -> int:
            raise AssertionError("dry-run must not create a batch")

    out = tmp_path / "reports"
    result = run_migration(
        root,
        out / "manifest.json",
        out / "report.json",
        dry_run=True,
        transport=ExplodingTransport(),
        scan_fn=lambda source_root, workers: ScanResult(
            files=[file_info], root_name=source_root.name
        ),
        dedup_fn=lambda scanned: fake_dedup(),
    )

    assert result.manifest.batch_id is None
    assert result.manifest.entry("dry-run.xls").status == "pending"
    assert (
        json.loads((out / "report.json").read_text(encoding="utf-8"))["summary"][
            "pending_count"
        ]
        == 1
    )


def test_gz_only_candidate_is_uploaded_and_completes_batch(tmp_path: Path) -> None:
    root = tmp_path / "估值表A"
    source = root / "gz" / "梦溪一号估值表" / "gz-only.xls"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"gz-only")
    file_info = FakeFile(
        "gz/梦溪一号估值表/gz-only.xls",
        source_zone="gz",
        sha256=hashlib.sha256(b"gz-only").hexdigest(),
        size_bytes=7,
    )
    transport = FakeTransport()

    result = run_migration(
        root,
        tmp_path / "reports" / "manifest.json",
        tmp_path / "reports" / "report.json",
        transport=transport,
        scan_fn=lambda source_root, workers: ScanResult(
            files=[file_info], root_name=source_root.name
        ),
        dedup_fn=lambda scanned: fake_dedup(),
    )

    entry = result.manifest.entry("gz-only.xls")
    assert entry.action == "import_gz_only"
    assert entry.status == "uploaded"
    assert transport.upload_calls == ["gz-only.xls"]
    assert transport.complete_calls == [41]
    assert result.ok is True


def test_http_transport_uses_session_cookie_and_sends_only_basename() -> None:
    requests: list[tuple[str, str, dict[str, str], bytes]] = []

    def request(
        method: str, url: str, headers: dict[str, str], body: bytes
    ) -> HttpResponse:
        requests.append((method, url, headers, body))
        if url.endswith("/api/v1/imports"):
            return HttpResponse(201, b'{"data":{"id":9}}')
        if url.endswith("/files"):
            return HttpResponse(201, b'{"data":{"id":10,"duplicate":false}}')
        return HttpResponse(200, b'{"data":{"status":"queued"}}')

    transport = HttpImportTransport(
        "https://example.invalid",
        token="test-token",
        request=request,
    )
    assert transport.create_batch("migration") == 9
    receipt = transport.upload_file(9, "估值表.xls", BytesIO(b"data"), 4)
    transport.complete_batch(9)

    assert receipt == UploadReceipt(remote_source_file_id=10, duplicate=False)
    assert len(requests) == 3
    assert all(
        "F:\\AgentWorks" not in body.decode("utf-8", errors="ignore")
        for *_, body in requests
    )
    assert 'filename="估值表.xls"' in requests[1][3].decode("utf-8")
    assert all(
        request_headers.get("Cookie") == f"{SESSION_COOKIE_NAME}=test-token"
        for _, _, request_headers, _ in requests
    )
    assert all(
        "Authorization" not in request_headers for _, _, request_headers, _ in requests
    )


def test_migration_cli_requires_session_token_for_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.delenv("MIGRATION_TOKEN", raising=False)

    result = main(
        [
            "--root",
            str(source_root),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--report",
            str(tmp_path / "report.json"),
            "--base-url",
            "https://example.invalid",
        ]
    )

    assert result == 2
    assert "MIGRATION_TOKEN" in capsys.readouterr().out


def test_report_does_not_accept_absolute_manifest_paths() -> None:
    payload = {
        "schema_version": 1,
        "root_name": "估值表A",
        "inventory_fingerprint": "fingerprint",
        "entries": [
            {
                "rel_path": "F:\\AgentWorks\\估值表A\\secret.xls",
                "action": "import",
            }
        ],
    }
    with pytest.raises(ValueError, match="relative"):
        MigrationManifest.from_dict(payload)
