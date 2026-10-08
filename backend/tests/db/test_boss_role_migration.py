"""Upgrade an old constrained role table containing accounts and dependants."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.db.session import create_engine


def test_boss_migration_preserves_accounts_sessions_and_grants(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{(tmp_path / 'boss.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("APP_ENV", "development")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    command.downgrade(config, "0012_user_fund_access")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO user_account (id, username, password_hash, role, status, failed_login_count, fund_access_mode) VALUES (1, 'legacy', 'test-hash', 'viewer', 'active', 0, 'selected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fund (id, standard_name, status) VALUES (1, 'Existing', 'active')"
            )
        )
        connection.execute(
            text("INSERT INTO user_fund_access (user_id, fund_id) VALUES (1, 1)")
        )
        connection.execute(
            text(
                "INSERT INTO user_session (user_id, token_hash, expires_at) VALUES (1, 'test-token', '2099-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO audit_log (actor_user_id, action, resource_type, result) VALUES (1, 'test', 'user', 'success')"
            )
        )
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE user_account SET role = 'boss'"))
    command.upgrade(config, "head")
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT role, fund_access_mode FROM user_account")
        ).one() == ("viewer", "selected")
        for table in ("user_fund_access", "user_session", "audit_log"):
            assert (
                connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                == 1
            )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert "ck_fundaccessmode" in {
            check["name"]
            for check in inspect(connection).get_check_constraints("user_account")
        }
        connection.execute(text("UPDATE user_account SET role = 'boss'"))
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE user_account SET role = 'unknown'"))
    with pytest.raises(RuntimeError, match="boss accounts"):
        command.downgrade(config, "0012_user_fund_access")
    with engine.begin() as connection:
        assert (
            connection.execute(text("SELECT role FROM user_account")).scalar_one()
            == "boss"
        )
        connection.execute(text("UPDATE user_account SET role = 'viewer'"))
    command.downgrade(config, "0012_user_fund_access")
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE user_account SET role = 'boss'"))
    engine.dispose()
