"""Mail synchronization orchestration over the existing import service."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import PureWindowsPath
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import AuditResult, JobStatus, SourceType
from app.db.models import AuditLog, BackgroundJob, ImportBatch, SourceMessage
from app.imports.service import ImportService

from .client import ImapClient, MailConnectionError, MailMessageError
from .config import MailSettings

VALUATION_EXTENSIONS = {".xls", ".xlsx"}
MAX_MESSAGE_ID_LENGTH = 255
MAX_ORIGINAL_FILENAME_LENGTH = 500
# Windows reserved device names (case-insensitive). Filenames like CON.xlsx
# or NUL.xlsx were accepted before, then the OS stripped the extension and
# routed the file to the corresponding device on Windows hosts.
WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


@dataclass(slots=True)
class _SyncCounters:
    messages_seen: int = 0
    messages_imported: int = 0
    messages_skipped: int = 0
    attachments_seen: int = 0
    attachments_imported: int = 0
    duplicate_attachments: int = 0
    ignored_attachments: int = 0
    failed_attachments: int = 0
    failed_messages: int = 0
    batches_created: int = 0
    bulk_header_round_trips: int = 0
    bulk_header_failed_chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "messages_seen": self.messages_seen,
            "messages_imported": self.messages_imported,
            "messages_skipped": self.messages_skipped,
            "attachments_seen": self.attachments_seen,
            "attachments_imported": self.attachments_imported,
            "duplicate_attachments": self.duplicate_attachments,
            "ignored_attachments": self.ignored_attachments,
            "failed_attachments": self.failed_attachments,
            "failed_messages": self.failed_messages,
            "batches_created": self.batches_created,
            "bulk_header_round_trips": self.bulk_header_round_trips,
            "bulk_header_failed_chunks": self.bulk_header_failed_chunks,
            "error_count": len(self.errors),
            "error_codes": sorted(set(self.errors)),
        }


@dataclass(frozen=True, slots=True)
class MailSyncResult:
    run_id: str
    status: str
    summary: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {"run_id": self.run_id, "status": self.status, **self.summary}


class MailSyncAlreadyRunning(RuntimeError):
    """Only one mailbox scan may run at a time."""


class MailService:
    """Fetch mail read-only and delegate every accepted file to ImportService."""

    def __init__(
        self,
        session: Session,
        *,
        settings: MailSettings,
        import_service: ImportService,
        client: ImapClient,
    ) -> None:
        self.session = session
        self.settings = settings
        self.import_service = import_service
        self.client = client

    @classmethod
    def from_app_settings(
        cls,
        session: Session,
        app_settings: Settings,
        mail_settings: MailSettings,
        *,
        connection_factory: Any = None,
    ) -> MailService:
        return cls(
            session,
            settings=mail_settings,
            import_service=ImportService.from_settings(session, app_settings),
            client=ImapClient(
                mail_settings,
                connection_factory=connection_factory,
            ),
        )

    def test_connection(self) -> None:
        self.settings.require_configured()
        with self.client.open() as connection:
            self.client.select_readonly(connection)

    def enqueue_sync(self, actor_user_id: int | None) -> MailSyncResult:
        self.settings.require_configured()
        active = self.session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == "mail_sync",
                BackgroundJob.status.in_(
                    (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.RETRY_DUE)
                ),
            )
            .order_by(BackgroundJob.id.desc())
            .limit(1)
        )
        if active is not None:
            raise MailSyncAlreadyRunning(active.resource_id)
        run_id, _job = self._start_run(actor_user_id, queued=True)
        return MailSyncResult(
            run_id=run_id, status="queued", summary=_SyncCounters().as_dict()
        )

    def sync(
        self,
        actor_user_id: int | None,
        *,
        run_id: str | None = None,
        job: BackgroundJob | None = None,
    ) -> MailSyncResult:
        self.settings.require_configured()
        if run_id is None or job is None:
            run_id, job = self._start_run(actor_user_id)
        counters = _SyncCounters()

        try:
            with self.client.open() as connection:
                self.client.select_readonly(connection)
                uids = self.client.list_uids(connection)
                counters.messages_seen = len(uids)
                # Pre-screen every message with bulk header fetches and one
                # batched database lookup, so a fully-known mailbox costs a
                # handful of round trips instead of one per message.
                external_ids = self._precompute_external_ids(connection, uids, counters)
                known_ids = self._known_external_ids(
                    {eid for eid in external_ids.values()}
                )
                for uid in uids:
                    if self._cancel_requested(job.id):
                        break
                    external_id = external_ids.get(uid)
                    if external_id is not None and external_id in known_ids:
                        counters.messages_skipped += 1
                        continue
                    try:
                        with self.session.begin_nested():
                            outcome = self._process_uid(
                                connection,
                                uid,
                                run_id,
                                actor_user_id,
                                counters,
                                external_id=external_id,
                            )
                        if outcome == "skipped":
                            counters.messages_skipped += 1
                        elif outcome == "imported":
                            counters.messages_imported += 1
                    except Exception:  # noqa: BLE001 - isolate one message
                        counters.failed_messages += 1
                        counters.errors.append("message_processing_failed")
                        self._record_audit(
                            action="mail.message_failed",
                            resource_type="mail_sync",
                            resource_id=run_id,
                            actor_user_id=actor_user_id,
                            summary={
                                "error_code": "message_processing_failed",
                                "uid": uid,
                            },
                            result=AuditResult.FAILURE,
                        )
        except MailConnectionError as exc:
            counters.failed_messages += 1
            counters.errors.append(str(exc))
        except Exception:  # noqa: BLE001 - connection failures are summarized
            counters.failed_messages += 1
            counters.errors.append("sync_failed")

        status = (
            "cancelled"
            if self._cancel_requested(job.id)
            else "failed"
            if counters.errors
            else "succeeded"
        )
        summary = counters.as_dict()
        self._finish_run(job, status, summary)
        return MailSyncResult(run_id=run_id, status=status, summary=summary)

    @staticmethod
    def list_sync_runs(session: Session, limit: int = 20) -> list[dict[str, object]]:
        active_jobs = session.scalars(
            select(BackgroundJob)
            .where(
                BackgroundJob.job_type == "mail_sync",
                BackgroundJob.status.in_((JobStatus.PENDING, JobStatus.RUNNING)),
            )
            .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
            .limit(min(max(limit, 1), 100))
        ).all()
        audits = session.scalars(
            select(AuditLog)
            .where(
                AuditLog.resource_type == "mail_sync",
                AuditLog.action.in_(
                    ("mail.sync_completed", "mail.sync_failed", "mail.sync_cancelled")
                ),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(min(max(limit, 1), 100))
        ).all()
        active = [
            {
                "run_id": job.resource_id,
                "status": "running" if job.status == JobStatus.RUNNING else "queued",
                "created_at": job.created_at,
                **_SyncCounters().as_dict(),
            }
            for job in active_jobs
        ]
        completed = [
            {
                "run_id": audit.resource_id,
                "status": "failed"
                if audit.action == "mail.sync_failed"
                else "cancelled"
                if audit.action == "mail.sync_cancelled"
                else "succeeded",
                "created_at": audit.created_at,
                **MailService._public_summary(audit.summary),
            }
            for audit in audits
        ]
        return (active + completed)[:limit]

    def _start_run(
        self, actor_user_id: int | None, *, queued: bool = False
    ) -> tuple[str, BackgroundJob]:
        run_id = uuid4().hex
        now = datetime.now(UTC)
        job = BackgroundJob(
            job_type="mail_sync",
            resource_id=run_id,
            status=JobStatus.PENDING if queued else JobStatus.RUNNING,
            attempts=1,
            max_attempts=1,
            started_at=now,
            locked_at=now,
        )
        self.session.add(job)
        self._record_audit(
            action="mail.sync_started",
            resource_type="mail_sync",
            resource_id=run_id,
            actor_user_id=actor_user_id,
        )
        self.session.flush()
        return run_id, job

    def _finish_run(
        self, job: BackgroundJob, status: str, summary: dict[str, object]
    ) -> None:
        job.status = (
            JobStatus.SUCCEEDED
            if status in {"succeeded", "cancelled"}
            else JobStatus.FAILED
        )
        job.finished_at = datetime.now(UTC)
        job.locked_at = None
        job.lease_token = None
        job.next_retry_at = None
        job.error_code = (
            None if status in {"succeeded", "cancelled"} else "mail_sync_failed"
        )
        self._record_audit(
            action="mail.sync_completed"
            if status == "succeeded"
            else "mail.sync_cancelled"
            if status == "cancelled"
            else "mail.sync_failed",
            resource_type="mail_sync",
            resource_id=job.resource_id,
            actor_user_id=None,
            summary=summary,
            result=AuditResult.FAILURE if status == "failed" else AuditResult.SUCCESS,
        )
        self.session.flush()

    def _cancel_requested(self, job_id: int) -> bool:
        return bool(
            self.session.scalar(
                select(BackgroundJob.cancel_requested).where(BackgroundJob.id == job_id)
            )
        )

    def _precompute_external_ids(
        self,
        connection: Any,
        uids: list[str],
        counters: _SyncCounters,
    ) -> dict[str, str]:
        """Bulk-fetch Message-ID headers and map each UID to its external id.

        If a bulk chunk fails, the caller falls back to per-UID header
        fetches in ``_process_uid`` for the affected UIDs. The failure is
        recorded in ``counters`` so operators can see it in the sync summary.
        """

        external_ids: dict[str, str] = {}
        if not uids:
            return external_ids
        chunk_count = (len(uids) + 499) // 500
        try:
            header_map = self.client.fetch_headers_bulk(connection, uids)
            counters.bulk_header_round_trips += chunk_count
        except MailMessageError:
            header_map = {}
            counters.bulk_header_failed_chunks += chunk_count
            counters.errors.append("bulk_header_fetch_failed")
        for uid in uids:
            raw_headers = header_map.get(uid)
            if raw_headers is None:
                # UID missing from the bulk response; per-UID fetch in
                # _process_uid keeps the message correct.
                continue
            external_ids[uid] = self._extract_message_id_from_headers(raw_headers, uid)
        return external_ids

    def _known_external_ids(self, candidates: set[str]) -> set[str]:
        """Return which candidate external ids already exist in the database."""

        known: set[str] = set()
        ordered = sorted(candidates)
        for start in range(0, len(ordered), 500):
            chunk = ordered[start : start + 500]
            known.update(
                self.session.scalars(
                    select(SourceMessage.external_message_id).where(
                        SourceMessage.external_message_id.in_(chunk)
                    )
                )
            )
        return known

    def _process_uid(
        self,
        connection: Any,
        uid: str,
        run_id: str,
        actor_user_id: int | None,
        counters: _SyncCounters,
        *,
        external_id: str | None = None,
    ) -> str:
        # Phase 1: lightweight header fetch, only when not precomputed in bulk
        if external_id is None:
            try:
                raw_headers = self.client.fetch_headers(connection, uid)
            except MailMessageError:
                counters.failed_messages += 1
                counters.errors.append("message_fetch_failed")
                self._record_audit(
                    action="mail.message_failed",
                    resource_type="mail_sync",
                    resource_id=run_id,
                    actor_user_id=actor_user_id,
                    summary={"error_code": "message_fetch_failed", "uid": uid},
                    result=AuditResult.FAILURE,
                )
                return "failed"
            external_id = self._extract_message_id_from_headers(raw_headers, uid)
            if self.session.scalar(
                select(SourceMessage.id).where(
                    SourceMessage.external_message_id == external_id
                )
            ):
                return "skipped"

        # Phase 2: full message fetch only for new messages
        try:
            raw_message = self.client.fetch_message(connection, uid)
        except MailMessageError:
            counters.failed_messages += 1
            counters.errors.append("message_fetch_failed")
            self._record_audit(
                action="mail.message_failed",
                resource_type="mail_sync",
                resource_id=run_id,
                actor_user_id=actor_user_id,
                summary={"error_code": "message_fetch_failed", "uid": uid},
                result=AuditResult.FAILURE,
            )
            return "failed"
        message = BytesParser(policy=policy.default).parsebytes(raw_message)

        source_message = SourceMessage(
            external_message_id=external_id,
            sender=self._clip(str(message.get("From", "")), 255),
            subject=self._clip(self._header_text(message.get("Subject")), 500),
            received_at=self._received_at(message),
            sync_batch=run_id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(source_message)
                self.session.flush()
        except IntegrityError:
            if self.session.scalar(
                select(SourceMessage.id).where(
                    SourceMessage.external_message_id == external_id
                )
            ):
                return "skipped"
            raise

        batch: ImportBatch | None = None
        total_attachment_bytes = 0
        attachment_count = 0
        for part in message.walk():
            filename_header = part.get_filename()
            if filename_header is None:
                continue
            attachment_count += 1
            counters.attachments_seen += 1
            if attachment_count > self.settings.max_attachment_count:
                counters.ignored_attachments += 1
                self._record_attachment_issue(
                    source_message,
                    run_id,
                    "attachments_limit_exceeded",
                    actor_user_id,
                )
                continue
            outcome, batch, total_attachment_bytes = self._process_attachment(
                part,
                filename_header,
                source_message,
                actor_user_id,
                batch,
                total_attachment_bytes,
                run_id,
                counters,
            )
            if outcome == "imported":
                counters.attachments_imported += 1
            elif outcome == "duplicate":
                counters.duplicate_attachments += 1
            elif outcome == "ignored":
                counters.ignored_attachments += 1
            else:
                counters.failed_attachments += 1

        if batch is not None and batch.file_count:
            self.import_service.complete_batch(batch.id, actor_user_id)
        return "imported"

    def _process_attachment(
        self,
        part: Message,
        filename_header: str,
        source_message: SourceMessage,
        actor_user_id: int | None,
        batch: ImportBatch | None,
        total_attachment_bytes: int,
        run_id: str,
        counters: _SyncCounters,
    ) -> tuple[str, ImportBatch | None, int]:
        filename = self._decode_filename(filename_header)
        if not filename:
            filename = self._fallback_filename(part)
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            self._record_attachment_issue(
                source_message, run_id, "invalid_attachment_payload", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        payload_size = len(payload)
        total_attachment_bytes += payload_size
        if payload_size > self.settings.max_attachment_bytes:
            self._record_attachment_issue(
                source_message, run_id, "attachment_too_large", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        if total_attachment_bytes > self.settings.max_total_attachment_bytes:
            self._record_attachment_issue(
                source_message, run_id, "attachments_too_large", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        if not self._safe_filename(filename):
            self._record_attachment_issue(
                source_message, run_id, "unsafe_filename", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        extension = self._extension(filename)
        if extension not in VALUATION_EXTENSIONS:
            self._record_attachment_issue(
                source_message, run_id, "unsupported_extension", actor_user_id
            )
            return "ignored", batch, total_attachment_bytes

        if batch is None:
            batch = self.import_service.create_batch(
                SourceType.EMAIL, actor_user_id, source_message_id=source_message.id
            )
            counters.batches_created += 1
        try:
            upload = self.import_service.receive_upload(
                batch.id,
                filename,
                BytesIO(payload),
                actor_user_id,
            )
        except ImportService.FileTooLarge:
            self._record_attachment_issue(
                source_message, run_id, "attachment_too_large", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        except ImportService.InvalidFile:
            self._record_attachment_issue(
                source_message, run_id, "invalid_file", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        except ValueError:
            self._record_attachment_issue(
                source_message, run_id, "attachment_rejected", actor_user_id
            )
            return "failed", batch, total_attachment_bytes
        if upload.duplicate:
            self._record_attachment_issue(
                source_message, run_id, "duplicate_attachment", actor_user_id
            )
            return "duplicate", batch, total_attachment_bytes
        return "imported", batch, total_attachment_bytes

    def _record_attachment_issue(
        self,
        source_message: SourceMessage,
        run_id: str,
        code: str,
        actor_user_id: int | None,
    ) -> None:
        ignored = code in {
            "unsupported_extension",
            "duplicate_attachment",
            "attachments_limit_exceeded",
        }
        self._record_audit(
            action="mail.attachment_ignored" if ignored else "mail.attachment_failed",
            resource_type="source_message",
            resource_id=str(source_message.id),
            actor_user_id=actor_user_id,
            summary={"error_code": code, "sync_run_id": run_id},
            result=AuditResult.SUCCESS if ignored else AuditResult.FAILURE,
        )

    def _record_audit(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        actor_user_id: int | None,
        summary: dict[str, object] | None = None,
        result: AuditResult = AuditResult.SUCCESS,
    ) -> None:
        self.session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                summary=summary,
                result=result,
            )
        )

    @staticmethod
    def _message_id(message: Message, uid: str) -> str:
        raw_message_id = message.get("Message-ID")
        candidate = str(raw_message_id).strip() if raw_message_id else f"imap-uid:{uid}"
        if len(candidate) <= MAX_MESSAGE_ID_LENGTH:
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return f"message-id-sha256:{digest}"

    @staticmethod
    def _extract_message_id_from_headers(raw_headers: bytes, uid: str) -> str:
        """Extract Message-ID from raw header bytes without full parsing."""

        header_message = BytesParser(policy=policy.default).parsebytes(
            raw_headers, headersonly=True
        )
        raw_message_id = header_message.get("Message-ID")
        candidate = str(raw_message_id).strip() if raw_message_id else f"imap-uid:{uid}"
        if len(candidate) <= MAX_MESSAGE_ID_LENGTH:
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return f"message-id-sha256:{digest}"

    @staticmethod
    def _header_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _received_at(message: Message) -> datetime:
        raw_date = message.get("Date")
        if raw_date:
            try:
                parsed = parsedate_to_datetime(str(raw_date))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                pass
        return datetime.now(UTC)

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        return value[:limit]

    @staticmethod
    def _decode_filename(value: str) -> str:
        pieces: list[str] = []
        try:
            decoded = decode_header(value)
        except (TypeError, ValueError):
            return ""
        # Single-byte "passthrough" encodings accept every byte and produce
        # mojibake when the bytes are actually UTF-8 or GBK. Detect that by
        # re-decoding the bytes as UTF-8 — if it also yields a valid string,
        # the declared charset is wrong. Fall back to the caller to mint a
        # token-based filename.
        passthrough_charsets = {
            "iso-8859-1",
            "iso8859-1",
            "latin-1",
            "latin1",
            "ascii",
            "us-ascii",
        }
        for piece, charset in decoded:
            if isinstance(piece, bytes):
                piece_text = None
                if charset:
                    try:
                        piece_text = piece.decode(charset, errors="strict")
                    except (LookupError, UnicodeError):
                        piece_text = None
                if piece_text is None:
                    try:
                        piece_text = piece.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        return ""
                elif (charset or "").lower() in passthrough_charsets:
                    try:
                        utf8_text = piece.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        utf8_text = None
                    if utf8_text is not None:
                        return ""
                pieces.append(piece_text)
            else:
                pieces.append(piece)
        return "".join(pieces).strip()

    @classmethod
    def _fallback_filename(cls, part: Message) -> str:
        """Return a safe deterministic filename when the declared one is unusable."""

        extension = cls._extension(part.get_filename() or "")
        if not extension or extension not in VALUATION_EXTENSIONS:
            extension = ".xlsx"
        return f"unnamed-{secrets.token_hex(8)}{extension}"

    @staticmethod
    def _safe_filename(filename: str) -> bool:
        if (
            not filename
            or len(filename) > MAX_ORIGINAL_FILENAME_LENGTH
            or filename in {".", ".."}
        ):
            return False
        if any(character in filename for character in ("\x00", "/", "\\")):
            return False
        # Trailing dots and spaces: Windows silently strips them, so
        # "report.xlsx." and "report.xlsx " both collide with "report.xlsx".
        if filename != filename.rstrip(". "):
            return False
        # Reject Windows reserved device names (case-insensitive). The OS
        # silently strips the extension, so CON.xlsx -> CON, NUL.xlsx -> NUL.
        bare = filename.rsplit(".", 1)[0].upper()
        if bare in WINDOWS_RESERVED_BASENAMES:
            return False
        windows_path = PureWindowsPath(filename)
        return not windows_path.drive and windows_path.name == filename

    @staticmethod
    def _extension(filename: str) -> str:
        dot = filename.rfind(".")
        return filename[dot:].lower() if dot > 0 else ""

    @staticmethod
    def _public_summary(summary: dict[str, object] | None) -> dict[str, object]:
        if not summary:
            return {}
        allowed = {
            "messages_seen",
            "messages_imported",
            "messages_skipped",
            "attachments_seen",
            "attachments_imported",
            "duplicate_attachments",
            "ignored_attachments",
            "failed_attachments",
            "failed_messages",
            "batches_created",
            "bulk_header_round_trips",
            "bulk_header_failed_chunks",
            "error_count",
            "error_codes",
        }
        return {key: value for key, value in summary.items() if key in allowed}
