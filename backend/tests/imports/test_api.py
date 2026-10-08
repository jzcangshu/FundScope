from io import BytesIO

from fastapi.testclient import TestClient

from .conftest import make_xlsx_bytes


def test_import_batch_upload_complete_and_get(admin_client: TestClient) -> None:
    created = admin_client.post("/api/v1/imports", json={"source_type": "upload"})
    batch_id = created.json()["data"]["id"]

    uploaded = admin_client.post(
        f"/api/v1/imports/{batch_id}/files",
        files={
            "file": (
                "valuation.xlsx",
                BytesIO(make_xlsx_bytes()),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    completed = admin_client.post(f"/api/v1/imports/{batch_id}/complete")
    detail = admin_client.get(f"/api/v1/imports/{batch_id}")

    assert created.status_code == 201
    assert uploaded.status_code == 201
    assert uploaded.json()["data"]["duplicate"] is False
    assert completed.status_code == 200
    assert completed.json()["data"]["job"]["status"] == "pending"
    assert detail.json()["data"]["file_count"] == 1


def test_operator_can_import_and_viewer_cannot(admin_client: TestClient) -> None:
    admin_client.post(
        "/api/v1/users",
        json={"username": "operator", "password": "correct horse", "role": "operator"},
    )
    admin_client.post(
        "/api/v1/users",
        json={"username": "viewer", "password": "correct horse", "role": "viewer"},
    )
    operator = TestClient(admin_client.app)
    viewer = TestClient(admin_client.app)
    operator.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": "correct horse"},
    )
    viewer.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct horse"},
    )

    allowed = operator.post("/api/v1/imports", json={"source_type": "upload"})
    denied_create = viewer.post("/api/v1/imports", json={"source_type": "upload"})
    denied_get = viewer.get(f"/api/v1/imports/{allowed.json()['data']['id']}")

    assert allowed.status_code == 201
    assert denied_create.status_code == 403
    assert denied_get.status_code == 403
