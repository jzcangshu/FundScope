from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from email.message import EmailMessage
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_db
from app.db.base import Base
from app.db.session import create_engine
from app.main import create_app


def make_xlsx_bytes(payload: bytes = b"worksheet") -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types />")
        archive.writestr("xl/workbook.xml", b"<workbook />")
        archive.writestr("xl/worksheets/sheet1.xml", payload)
    return output.getvalue()


def make_email(
    message_id: str,
    attachments: list[tuple[str, bytes]],
    *,
    sender: str = "sender@example.test",
    date: str = "Wed, 26 Aug 2026 10:00:00 +0000",
) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["From"] = sender
    message["Subject"] = "valuation"
    message["Date"] = date
    message.set_content("attached")
    for filename, payload in attachments:
        message.add_attachment(
            payload,
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
    return bytes(message)


def _message_id_header(raw_message: bytes) -> bytes:
    """Extract the raw Message-ID header block the way a server would return it."""

    from email import policy
    from email.parser import BytesParser

    parsed = BytesParser(policy=policy.default).parsebytes(raw_message)
    message_id = parsed.get("Message-ID")
    if message_id is None:
        return b"\r\n"
    return f"Message-ID: {message_id}\r\n".encode()


class FakeConnection:
    def __init__(self, messages: dict[str, bytes | None]) -> None:
        self.messages = messages
        self.logged_in_with: tuple[str, str] | None = None
        self.selected_readonly = False
        self.logged_out = False
        self.bulk_fetch_calls: list[str] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.logged_in_with = (username, password)
        return "OK", [b"logged in"]

    def select(self, mailbox: str, *, readonly: bool) -> tuple[str, list[bytes]]:
        assert mailbox == "INBOX"
        self.selected_readonly = readonly
        return "OK", [b"0"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command.upper() == "SEARCH":
            return "OK", [" ".join(self.messages).encode("ascii")]
        if command.upper() == "FETCH":
            uid = str(args[0])
            if ":" in uid:
                # Ranged fetch: return Message-ID headers like a real server.
                self.bulk_fetch_calls.append(uid)
                start, _, end = uid.partition(":")
                lo, hi = int(start), int(end)
                items: list[object] = []
                for candidate in map(str, range(lo, hi + 1)):
                    raw = self.messages.get(candidate)
                    if raw is None:
                        continue
                    items.append(
                        (
                            f"UID {candidate} BODY[HEADER.FIELDS (MESSAGE-ID)]".encode(),
                            _message_id_header(raw),
                        )
                    )
                return "OK", items
            raw_message = self.messages[uid]
            if raw_message is None:
                return "NO", [None]
            if "BODY[HEADER.FIELDS" in str(args[1]):
                return "OK", [
                    (
                        f"UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)]".encode(),
                        _message_id_header(raw_message),
                    )
                ]
            return "OK", [(b"RFC822", raw_message)]
        raise AssertionError(command)

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logged out"]


class FakeMailbox:
    def __init__(self, messages: dict[str, bytes | None] | None = None) -> None:
        self.messages = messages or {}
        self.connections: list[FakeConnection] = []

    def __call__(self, _settings: object) -> FakeConnection:
        connection = FakeConnection(self.messages)
        self.connections.append(connection)
        return connection


@pytest.fixture()
def fake_mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture()
def app_and_engine(
    tmp_path, fake_mailbox: FakeMailbox
) -> Iterator[tuple[FastAPI, object]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app()
    app.state.settings = replace(
        app.state.settings,
        environment="test",
        upload_temp_dir=str(tmp_path / "temp"),
        source_storage_dir=str(tmp_path / "source"),
        max_upload_bytes=1024 * 1024,
    )
    app.state.mail_connection_factory = fake_mailbox

    def override_get_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app, engine
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client(app_and_engine: tuple[FastAPI, object]) -> Iterator[TestClient]:
    app, _ = app_and_engine
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("MAIL_IMAP_PORT", "993")
    monkeypatch.setenv("MAIL_IMAP_USERNAME", "funds@example.test")
    monkeypatch.setenv("MAIL_IMAP_PASSWORD", "test-only-authorisation-code")
    response = client.post(
        "/api/v1/auth/initialize",
        json={"username": "admin", "password": "correct horse"},
    )
    assert response.status_code == 201
    return client
