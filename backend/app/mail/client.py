"""Small standard-library IMAP adapter with a read-only mailbox contract."""

from __future__ import annotations

import imaplib
import re
import ssl
from collections.abc import Callable
from typing import Any

from .config import MailSettings


class MailConnectionError(RuntimeError):
    """Raised for a connection or mailbox operation failure."""


class MailMessageError(RuntimeError):
    """Raised when one message cannot be fetched safely."""


def _default_connection(settings: MailSettings) -> Any:
    if settings.use_ssl:
        return imaplib.IMAP4_SSL(
            settings.host,
            settings.port,
            timeout=settings.timeout_seconds,
            ssl_context=ssl.create_default_context(),
        )
    return imaplib.IMAP4(settings.host, settings.port, timeout=settings.timeout_seconds)


class _ManagedConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def __enter__(self) -> Any:
        return self.connection

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        try:
            self.connection.logout()
        except Exception:  # noqa: BLE001 - logout must not mask sync results
            return
        return


class ImapClient:
    """Expose only read-only IMAP operations needed by mail synchronization."""

    def __init__(
        self,
        settings: MailSettings,
        *,
        connection_factory: Callable[[MailSettings], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.connection_factory = connection_factory or _default_connection

    def open(self) -> _ManagedConnection:
        self.settings.require_configured()
        try:
            connection = self.connection_factory(self.settings)
            status, _ = connection.login(self.settings.username, self.settings.password)
            if status != "OK":
                raise MailConnectionError("imap_login_failed")
        except MailConnectionError:
            raise
        except Exception as exc:
            raise MailConnectionError("imap_connection_failed") from exc
        return _ManagedConnection(connection)

    def select_readonly(self, connection: Any) -> None:
        try:
            status, _ = connection.select(self.settings.mailbox, readonly=True)
        except Exception as exc:
            raise MailConnectionError("imap_select_failed") from exc
        if status != "OK":
            raise MailConnectionError("imap_select_failed")

    def list_uids(self, connection: Any) -> list[str]:
        try:
            status, data = connection.uid("search", None, "ALL")
        except Exception as exc:
            raise MailConnectionError("imap_search_failed") from exc
        if status != "OK":
            raise MailConnectionError("imap_search_failed")
        if not data or not data[0]:
            return []
        raw_uids = data[0].split() if isinstance(data[0], bytes) else data[0]
        return [
            item.decode("ascii", errors="strict")
            if isinstance(item, bytes)
            else str(item)
            for item in raw_uids
        ]

    def fetch_message(self, connection: Any, uid: str) -> bytes:
        try:
            status, data = connection.uid("fetch", uid, "(RFC822)")
        except Exception as exc:
            raise MailMessageError("imap_fetch_failed") from exc
        if status != "OK":
            raise MailMessageError("imap_fetch_failed")

        for item in data or []:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[1], bytes)
            ):
                raw_message = item[1]
                if len(raw_message) > self.settings.max_message_bytes:
                    raise MailMessageError("message_too_large")
                return raw_message
        raise MailMessageError("imap_fetch_failed")

    def fetch_headers(self, connection: Any, uid: str) -> bytes:
        """Fetch only the Message-ID header for lightweight deduplication."""

        try:
            status, data = connection.uid(
                "fetch", uid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])"
            )
        except Exception as exc:
            raise MailMessageError("imap_fetch_failed") from exc
        if status != "OK":
            raise MailMessageError("imap_fetch_failed")
        for item in data or []:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[1], bytes)
            ):
                return item[1]
        raise MailMessageError("imap_fetch_failed")

    def fetch_headers_bulk(
        self,
        connection: Any,
        uids: list[str],
        *,
        chunk_size: int = 500,
    ) -> dict[str, bytes]:
        """Fetch Message-ID headers for many UIDs using ranged FETCH commands.

        One round trip covers ``chunk_size`` messages, so scanning a full
        mailbox takes seconds instead of one network round trip per message.
        UIDs missing from the server response are simply absent from the
        returned mapping; callers fall back to per-UID fetch for those.
        """

        result: dict[str, bytes] = {}
        for start in range(0, len(uids), chunk_size):
            chunk = uids[start : start + chunk_size]
            range_spec = f"{chunk[0]}:{chunk[-1]}"
            try:
                status, data = connection.uid(
                    "fetch", range_spec, "(UID BODY[HEADER.FIELDS (MESSAGE-ID)])"
                )
            except Exception as exc:
                raise MailMessageError("imap_fetch_failed") from exc
            if status != "OK":
                raise MailMessageError("imap_fetch_failed")
            for item in data or []:
                if not (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and isinstance(item[0], bytes)
                    and isinstance(item[1], bytes)
                ):
                    continue
                match = re.search(rb"UID (\d+)", item[0])
                if match is None:
                    continue
                uid = match.group(1).decode("ascii", errors="strict")
                result[uid] = item[1]
        return result
