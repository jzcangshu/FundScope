"""Environment-backed, non-persistent IMAP configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .credential_store import MailCredentialStoreError, read_mail_credential


class MailConfigurationError(ValueError):
    """Raised when mail configuration is absent or malformed."""


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MailConfigurationError("invalid_mail_config")


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise MailConfigurationError("invalid_mail_config") from exc
    if not minimum <= parsed <= maximum:
        raise MailConfigurationError("invalid_mail_config")
    return parsed


@dataclass(frozen=True, slots=True)
class MailSettings:
    """Runtime IMAP settings; the password is deliberately excluded from repr."""

    host: str = ""
    port: int = 993
    username: str = ""
    password: str = field(default="", repr=False)
    use_ssl: bool = True
    mailbox: str = "INBOX"
    timeout_seconds: int = 15
    max_attachment_count: int = 1_000
    max_attachment_bytes: int = 20 * 1024 * 1024
    max_total_attachment_bytes: int = 50 * 1024 * 1024
    max_message_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_environment(cls, *, username_override: str | None = None) -> MailSettings:
        try:
            password = read_mail_credential()
        except MailCredentialStoreError as exc:
            raise MailConfigurationError(str(exc)) from exc

        return cls(
            host=os.getenv("MAIL_IMAP_HOST", "").strip(),
            port=_read_int("MAIL_IMAP_PORT", 993, 1, 65_535),
            username=(
                username_override.strip()
                if username_override is not None
                else os.getenv("MAIL_IMAP_USERNAME", "").strip()
            ),
            password=password,
            use_ssl=_read_bool("MAIL_IMAP_USE_SSL", True),
            mailbox=os.getenv("MAIL_IMAP_MAILBOX", "INBOX"),
            timeout_seconds=_read_int("MAIL_IMAP_TIMEOUT_SECONDS", 15, 1, 120),
            max_attachment_count=_read_int(
                "MAIL_IMAP_MAX_ATTACHMENT_COUNT", 1_000, 1, 10_000
            ),
            max_attachment_bytes=_read_int(
                "MAIL_IMAP_MAX_ATTACHMENT_BYTES",
                20 * 1024 * 1024,
                1,
                20 * 1024 * 1024,
            ),
            max_total_attachment_bytes=_read_int(
                "MAIL_IMAP_MAX_TOTAL_ATTACHMENT_BYTES",
                50 * 1024 * 1024,
                1,
                100 * 1024 * 1024,
            ),
            max_message_bytes=_read_int(
                "MAIL_IMAP_MAX_MESSAGE_BYTES",
                64 * 1024 * 1024,
                1,
                100 * 1024 * 1024,
            ),
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username and self.password)

    def require_configured(self) -> None:
        if not self.configured:
            raise MailConfigurationError("mail_not_configured")
