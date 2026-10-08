from __future__ import annotations

from dataclasses import replace

from app import worker
from app.config import get_settings


def test_worker_processes_one_job_and_disposes_engine(monkeypatch) -> None:
    settings = replace(get_settings(), database_url="sqlite+pysqlite:///:memory:")

    class FakeEngine:
        def dispose(self) -> None:
            calls.append("disposed")

    engine = FakeEngine()
    calls: list[object] = []

    class FakeSession:
        def __init__(self, active_engine) -> None:
            assert active_engine is engine

        def __enter__(self):
            calls.append("entered")
            return self

        def __exit__(self, *args: object) -> None:
            calls.append("exited")

    monkeypatch.setattr(worker, "create_engine", lambda _: engine)
    monkeypatch.setattr(worker, "Session", FakeSession)
    monkeypatch.setattr(
        worker, "process_next_job", lambda session, runtime: (object(), None)
    )

    assert worker.run_worker(settings, max_jobs=1) == 1
    assert calls == ["entered", "exited", "disposed"]


def test_worker_stops_without_sleep_when_bounded_and_idle(monkeypatch) -> None:
    settings = replace(get_settings(), database_url="sqlite+pysqlite:///:memory:")

    class FakeEngine:
        def dispose(self) -> None:
            return None

    engine = FakeEngine()

    class FakeSession:
        def __init__(self, active_engine) -> None:
            assert active_engine is engine

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(worker, "create_engine", lambda _: engine)
    monkeypatch.setattr(worker, "Session", FakeSession)
    monkeypatch.setattr(worker, "process_next_job", lambda session, runtime: None)
    assert (
        worker.run_worker(
            settings, max_jobs=1, sleep=lambda _: (_ for _ in ()).throw(AssertionError)
        )
        == 0
    )
