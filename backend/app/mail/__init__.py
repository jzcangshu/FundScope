"""IMAP-backed mail ingestion for valuation attachments."""

from .client import ImapClient, MailConnectionError, MailMessageError
from .config import MailConfigurationError, MailSettings
from .credential_store import (
    MailCredentialStatus,
    MailCredentialStoreError,
    mail_credential_status,
    write_mail_credential,
)
from .service import MailService, MailSyncAlreadyRunning, MailSyncResult

__all__ = [
    "ImapClient",
    "MailConfigurationError",
    "MailConnectionError",
    "MailCredentialStatus",
    "MailCredentialStoreError",
    "MailMessageError",
    "MailService",
    "MailSettings",
    "MailSyncAlreadyRunning",
    "MailSyncResult",
    "mail_credential_status",
    "write_mail_credential",
]
