"""Controlled file storage for the IMAP authorization code."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


class MailCredentialStoreError(ValueError):
    """Raised when the configured credential source cannot be safely updated."""


@dataclass(frozen=True, slots=True)
class MailCredentialStatus:
    configured: bool
    source: str
    writable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "credential_source": self.source,
            "credential_writable": self.writable,
        }


def _credential_path() -> Path | None:
    raw_path = os.getenv("MAIL_IMAP_PASSWORD_FILE", "").strip()
    return Path(raw_path) if raw_path else None


def read_mail_credential() -> str:
    environment_value = os.getenv("MAIL_IMAP_PASSWORD", "")
    if environment_value:
        return environment_value
    path = _credential_path()
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise MailCredentialStoreError("mail_password_unavailable") from exc


def mail_credential_status() -> MailCredentialStatus:
    if os.getenv("MAIL_IMAP_PASSWORD", ""):
        return MailCredentialStatus(True, "environment", False)

    path = _credential_path()
    if path is None:
        return MailCredentialStatus(False, "none", False)
    try:
        configured = bool(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        configured = False
    except (OSError, UnicodeError):
        return MailCredentialStatus(False, "secret_file", False)

    parent = path.parent
    writable = parent.is_dir() and os.access(parent, os.W_OK)
    if path.exists():
        writable = writable and path.is_file() and os.access(path, os.W_OK)
    return MailCredentialStatus(
        configured,
        "secret_file" if configured or path.exists() else "none",
        writable,
    )


def write_mail_credential(authorization_code: str) -> MailCredentialStatus:
    if os.getenv("MAIL_IMAP_PASSWORD", ""):
        raise MailCredentialStoreError("mail_credential_managed_by_environment")
    path = _credential_path()
    if path is None:
        raise MailCredentialStoreError("mail_credential_file_not_configured")
    parent = path.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise MailCredentialStoreError("mail_credential_file_unavailable")

    temporary_path = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(authorization_code)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise MailCredentialStoreError("mail_credential_file_unavailable") from exc
    return MailCredentialStatus(True, "secret_file", True)


__all__ = [
    "MailCredentialStatus",
    "MailCredentialStoreError",
    "mail_credential_status",
    "read_mail_credential",
    "write_mail_credential",
]
