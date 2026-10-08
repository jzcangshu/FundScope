from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.base import ImportBatchStatus, JobStatus, SourceType
from app.db.models import (
    AuditLog,
    BackgroundJob,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
)

from ..imports.conftest import make_xlsx_bytes


def test_import_list_source_download_and_validation_endpoint(
    admin_client, app_and_engine
) -> None:
    content = make_xlsx_bytes()
    uploaded = admin_client.post(
        "/api/v1/imports",
        json={"source_type": "upload"},
    )
    assert uploaded.status_code == 201
    batch_id = uploaded.json()["data"]["id"]
    file_response = admin_client.post(
        f"/api/v1/imports/{batch_id}/files",
        files={
            "file": (
                "valuation.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert file_response.status_code == 201
    source_file_id = file_response.json()["data"]["id"]
    completed = admin_client.post(f"/api/v1/imports/{batch_id}/complete")
    assert completed.status_code == 200

    listed = admin_client.get("/api/v1/imports", params={"page": 1, "page_size": 10})
    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] == 1
    assert listed.json()["data"][0]["job"]["status"] == "pending"

    validations = admin_client.get(f"/api/v1/imports/{batch_id}/validations")
    assert validations.status_code == 200
    assert validations.json()["data"] == []

    source = admin_client.get(f"/api/v1/imports/{batch_id}/source/{source_file_id}")
    assert source.status_code == 200
    assert source.content == content

    with Session(app_and_engine[1]) as session:
        assert (
            session.scalar(
                __import__("sqlalchemy")
                .select(AuditLog.id)
                .where(AuditLog.action == "import.source_download")
            )
            is not None
        )


def test_import_list_reports_total_and_empty_out_of_range_page(admin_client) -> None:
    for _ in range(3):
        created = admin_client.post(
            "/api/v1/imports",
            json={"source_type": "upload"},
        )
        assert created.status_code == 201

    last_page = admin_client.get("/api/v1/imports", params={"page": 2, "page_size": 2})
    out_of_range = admin_client.get(
        "/api/v1/imports", params={"page": 3, "page_size": 2}
    )

    assert last_page.status_code == 200
    assert len(last_page.json()["data"]) == 1
    assert last_page.json()["meta"] == {"page": 2, "page_size": 2, "total": 3}
    assert out_of_range.status_code == 200
    assert out_of_range.json()["data"] == []
    assert out_of_range.json()["meta"] == {
        "page": 3,
        "page_size": 2,
        "total": 3,
    }


def test_failed_import_can_be_manually_retried(admin_client, app_and_engine) -> None:
    with Session(app_and_engine[1]) as session:
        batch = ImportBatch(
            source_type=SourceType.UPLOAD,
            status=ImportBatchStatus.FAILED,
            file_count=1,
        )
        session.add(batch)
        session.flush()
        job = BackgroundJob(
            job_type="process_import_batch",
            resource_id=str(batch.id),
            status=JobStatus.FAILED,
            attempts=3,
            max_attempts=3,
            error_code="max_attempts_exceeded",
        )
        session.add(job)
        session.commit()
        batch_id = batch.id

    retried = admin_client.post(f"/api/v1/imports/{batch_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] == "queued"
    assert retried.json()["data"]["job"]["status"] == "pending"
    assert retried.json()["data"]["job"]["attempts"] == 0


def test_import_source_endpoint_never_accepts_path_escape(
    admin_client, app_and_engine
) -> None:
    with Session(app_and_engine[1]) as session:
        batch = ImportBatch(source_type=SourceType.UPLOAD, file_count=1)
        session.add(batch)
        session.flush()
        source = SourceFile(
            original_filename="escape.xlsx",
            file_hash="a" * 64,
            file_size=1,
            file_extension=".xlsx",
            source_type=SourceType.UPLOAD,
            object_name="../escape.xlsx",
        )
        session.add(source)
        session.flush()
        session.add(ImportBatchFile(batch_id=batch.id, source_file_id=source.id))
        session.commit()
        batch_id = batch.id
        source_id = source.id

    response = admin_client.get(f"/api/v1/imports/{batch_id}/source/{source_id}")
    assert response.status_code == 404
