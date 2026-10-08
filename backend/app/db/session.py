"""SQLAlchemy engine and session construction helpers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings


def create_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an engine without opening a database connection."""

    url = database_url or get_settings().database_url
    engine_kwargs: dict[str, object] = {"echo": echo, "future": True}

    if url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            engine_kwargs["poolclass"] = StaticPool

    engine = sqlalchemy_create_engine(url, **engine_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_session(engine: Engine | None = None) -> Iterator[Session]:
    """Yield one transaction-ready session for dependency injection."""

    active_engine = engine or create_engine()
    with Session(active_engine) as session:
        yield session
