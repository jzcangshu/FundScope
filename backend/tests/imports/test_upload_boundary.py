from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.base import Base
from app.db.session import create_engine
from app.main import create_app

from .conftest import make_xlsx_bytes

BOUNDARY = "test-upload-boundary"
PREFIX = (
    f"--{BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="file"; filename="valuation.xlsx"\r\n'
    "Content-Type: application/octet-stream\r\n\r\n"
).encode()
SUFFIX = f"\r\n--{BOUNDARY}--\r\n".encode()


@pytest.fixture()
def concurrent_admin_client(tmp_path):
    # A StaticPool in-memory database shares one SQLite connection across all
    # request threads. Use a file database so concurrent requests get isolation.
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'concurrent.db').as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    app = create_app()
    app.state.settings = replace(
        app.state.settings,
        environment="test",
        database_url=database_url,
        upload_temp_dir=str(tmp_path / "temp"),
        source_storage_dir=str(tmp_path / "source"),
        max_upload_bytes=1024,
    )
    app.state.db_engine = engine
    try:
        with TestClient(app) as client:
            initialized = client.post(
                "/api/v1/auth/initialize",
                json={"username": "admin", "password": "correct horse"},
            )
            assert initialized.status_code == 201
            yield client
    finally:
        engine.dispose()


async def _stream_request(
    app, path, chunks, consumed, *, cookies=None, content_length=None
):
    async def body():
        for chunk in chunks:
            consumed.append(len(chunk))
            yield chunk

    headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies=cookies,
    ) as client:
        return await client.post(path, headers=headers, content=body())


def test_unauthenticated_upload_does_not_consume_the_request_body(app_and_engine):
    app, _ = app_and_engine
    chunks = [PREFIX, *([b"x" * 65536] * 32), SUFFIX]
    consumed = []

    response = asyncio.run(
        _stream_request(app, "/api/v1/imports/1/files", chunks, consumed)
    )

    assert response.status_code == 401
    assert consumed == []


def test_oversized_content_length_is_rejected_before_reading(admin_client):
    batch_id = admin_client.post("/api/v1/imports", json={}).json()["data"]["id"]
    consumed = []
    response = asyncio.run(
        _stream_request(
            admin_client.app,
            f"/api/v1/imports/{batch_id}/files",
            [PREFIX, make_xlsx_bytes(), SUFFIX],
            consumed,
            cookies=dict(admin_client.cookies),
            content_length=32 * 1024 * 1024,
        )
    )

    assert response.status_code == 413
    assert consumed == []
    assert (
        admin_client.get(f"/api/v1/imports/{batch_id}").json()["data"]["file_count"]
        == 0
    )


@pytest.mark.parametrize("concurrency", [1, 3])
@pytest.mark.parametrize("declared_length", [None, 1])
def test_chunked_uploads_are_bounded_and_close_partial_files(
    concurrent_admin_client, monkeypatch, concurrency, declared_length
):
    admin_client = concurrent_admin_client
    app = admin_client.app
    app.state.settings = replace(app.state.settings, max_upload_bytes=1024)
    batch_id = admin_client.post("/api/v1/imports", json={}).json()["data"]["id"]
    spools = []

    class TrackedSpool(BytesIO):
        def __init__(self, **kwargs):
            super().__init__()
            spools.append(self)

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedSpool)
    chunks = [PREFIX, *([b"x" * 16384] * 32), SUFFIX]
    consumed = [[] for _ in range(concurrency)]

    async def upload_all():
        return await asyncio.gather(
            *(
                _stream_request(
                    app,
                    f"/api/v1/imports/{batch_id}/files",
                    chunks,
                    reads,
                    cookies=dict(admin_client.cookies),
                    content_length=declared_length,
                )
                for reads in consumed
            )
        )

    responses = asyncio.run(upload_all())

    assert all(response.status_code == 413 for response in responses)
    assert all(sum(reads) < sum(map(len, chunks)) for reads in consumed)
    assert spools and all(spool.closed for spool in spools)
    assert (
        admin_client.get(f"/api/v1/imports/{batch_id}").json()["data"]["file_count"]
        == 0
    )


def test_multiple_files_in_one_request_are_rejected(admin_client):
    batch_id = admin_client.post("/api/v1/imports", json={}).json()["data"]["id"]
    response = admin_client.post(
        f"/api/v1/imports/{batch_id}/files",
        files=[
            ("file", ("first.xlsx", make_xlsx_bytes())),
            ("file", ("second.xlsx", make_xlsx_bytes())),
        ],
    )

    assert response.status_code == 400
    assert (
        admin_client.get(f"/api/v1/imports/{batch_id}").json()["data"]["file_count"]
        == 0
    )


@pytest.mark.parametrize(
    ("ending", "expected_status"),
    [(f"\r\n--{BOUNDARY}\r\ninvalid header\r\n".encode(), 400), (b"", 422)],
)
def test_malformed_multipart_is_rejected_and_closes_partial_files(
    admin_client, monkeypatch, ending, expected_status
):
    batch_id = admin_client.post("/api/v1/imports", json={}).json()["data"]["id"]
    spools = []

    class TrackedSpool(BytesIO):
        def __init__(self, **kwargs):
            super().__init__()
            spools.append(self)

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", TrackedSpool)
    response = admin_client.post(
        f"/api/v1/imports/{batch_id}/files",
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        content=PREFIX + b"partial" + ending,
    )

    assert response.status_code == expected_status
    assert spools and all(spool.closed for spool in spools)
