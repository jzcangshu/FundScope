from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_db
from app.db.base import Base
from app.db.session import create_engine
from app.main import create_app


@pytest.fixture()
def app_and_engine() -> Iterator[tuple[FastAPI, object]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app()

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
