from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import AuditResult, SourceType
from app.db.models import AuditLog, ImportBatch, SourceMessage

from .conftest import FakeMailbox, make_email, make_xlsx_bytes


def test_sync_links_batch_to_source_message(
    admin_client: TestClient,
    fake_mailbox: FakeMailbox,
    app_and_engine: tuple[object, object],
) -> None:
    fake_mailbox.messages["1"] = make_email(
        "<message-1@example.test>", [("valuation.xlsx", make_xlsx_bytes())]
    )

    response = admin_client.post("/api/v1/mail/sync")
    assert response.status_code == 200

    _, engine = app_and_engine
    with Session(engine) as session:
        message_id = session.scalar(select(SourceMessage.id))
        batch = session.scalar(
            select(ImportBatch).where(ImportBatch.source_type == SourceType.EMAIL)
        )
        assert batch is not None
        assert message_id is not None
        assert batch.source_message_id == message_id


def test_list_messages_reports_attachment_aggregates(
    admin_client: TestClient,
    fake_mailbox: FakeMailbox,
) -> None:
    payload = make_xlsx_bytes(b"aaaa")
    fake_mailbox.messages["1"] = make_email(
        "<message-1@example.test>",
        [("valuation.xlsx", payload)],
    )
    sync = admin_client.post("/api/v1/mail/sync")
    assert sync.status_code == 200

    listed = admin_client.get("/api/v1/mail/messages")
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert len(data) == 1
    assert data[0]["sender"] == "sender@example.test"
    assert data[0]["attachment_count"] == 1
    assert data[0]["total_size"] == len(payload)


def test_list_messages_reports_zero_attachments(
    admin_client: TestClient,
    fake_mailbox: FakeMailbox,
) -> None:
    fake_mailbox.messages["1"] = make_email(
        "<message-1@example.test>", [("notes.txt", b"ignore")]
    )
    sync = admin_client.post("/api/v1/mail/sync")
    assert sync.status_code == 200

    listed = admin_client.get("/api/v1/mail/messages")
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert len(data) == 1
    assert data[0]["attachment_count"] == 0
    assert data[0]["total_size"] == 0


def test_list_messages_orders_newest_first(
    admin_client: TestClient,
    fake_mailbox: FakeMailbox,
) -> None:
    fake_mailbox.messages.update(
        {
            "1": make_email(
                "<message-1@example.test>",
                [("one.xlsx", make_xlsx_bytes())],
                sender="older@example.test",
                date="Tue, 25 Aug 2026 10:00:00 +0000",
            ),
            "2": make_email(
                "<message-2@example.test>",
                [("two.xlsx", make_xlsx_bytes())],
                sender="newer@example.test",
                date="Thu, 27 Aug 2026 10:00:00 +0000",
            ),
        }
    )
    sync = admin_client.post("/api/v1/mail/sync")
    assert sync.status_code == 200

    listed = admin_client.get("/api/v1/mail/messages")
    assert listed.status_code == 200
    assert [row["sender"] for row in listed.json()["data"]] == [
        "newer@example.test",
        "older@example.test",
    ]


def test_archive_messages_returns_zip_of_attachments(
    admin_client: TestClient,
    fake_mailbox: FakeMailbox,
    app_and_engine: tuple[object, object],
) -> None:
    payload = make_xlsx_bytes(b"worksheet-a")
    fake_mailbox.messages["1"] = make_email(
        "<message-1@example.test>", [("valuation.xlsx", payload)]
    )
    sync = admin_client.post("/api/v1/mail/sync")
    assert sync.status_code == 200

    _, engine = app_and_engine
    with Session(engine) as session:
        message_id = session.scalar(select(SourceMessage.id))

    response = admin_client.post(
        "/api/v1/mail/messages/archive", json={"message_ids": [message_id]}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.namelist() == ["valuation.xlsx"]
        assert archive.read("valuation.xlsx") == payload

    with Session(engine) as session:
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "mail.attachments_downloaded")
        )
        assert audit is not None
        assert audit.result == AuditResult.SUCCESS


def test_archive_messages_without_attachments_returns_404(
    admin_client: TestClient,
) -> None:
    response = admin_client.post(
        "/api/v1/mail/messages/archive", json={"message_ids": [999999]}
    )
    assert response.status_code == 404


def test_archive_messages_404_when_stored_file_missing(
    admin_client: TestClient,
    fake_mailbox: FakeMailbox,
    app_and_engine: tuple[object, object],
) -> None:
    fake_mailbox.messages["1"] = make_email(
        "<message-1@example.test>", [("valuation.xlsx", make_xlsx_bytes())]
    )
    assert admin_client.post("/api/v1/mail/sync").status_code == 200

    app, engine = app_and_engine
    with Session(engine) as session:
        message_id = session.scalar(select(SourceMessage.id))

    stored = next(Path(app.state.settings.source_storage_dir).iterdir())
    stored.unlink()

    response = admin_client.post(
        "/api/v1/mail/messages/archive", json={"message_ids": [message_id]}
    )
    assert response.status_code == 404
