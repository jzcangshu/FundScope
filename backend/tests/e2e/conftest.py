from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_db
from app.db.base import Base
from app.db.session import create_engine
from app.main import create_app


@pytest.fixture()
def app_and_engine(tmp_path) -> Iterator[tuple[FastAPI, object]]:
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

    def override_get_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app, engine
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def admin_client(app_and_engine: tuple[FastAPI, object]) -> Iterator[TestClient]:
    app, _ = app_and_engine
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/initialize",
            json={"username": "admin", "password": "correct horse"},
        )
        assert response.status_code == 201
        yield client
