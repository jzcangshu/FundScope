"""Exercise upgrading existing accounts as well as fresh metadata migrations."""

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.db.session import create_engine


def test_upgrade_preserves_legacy_accounts_and_constrains_new_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'permissions.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "development")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.upgrade(config, "head")
    command.downgrade(config, "0011_benchmark_daily")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        assert "fund_access_mode" not in {
            column["name"] for column in inspect(connection).get_columns("user_account")
        }
        connection.execute(
            text(
                "INSERT INTO user_account (username, password_hash, role, status, failed_login_count) "
                "VALUES ('legacy', 'test-hash', 'operator', 'active', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fund (standard_name, status) VALUES ('Existing', 'active')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO source_file (original_filename, file_hash, file_size, file_extension, source_type, object_name) "
                "VALUES ('existing.xlsx', :hash, 10, '.xlsx', 'upload', 'old-object')"
            ),
            {"hash": "a" * 64},
        )
    command.upgrade(config, "head")
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT fund_access_mode FROM user_account")
            ).scalar_one()
            == "all"
        )
        assert (
            connection.execute(
                text("SELECT recognized_fund_id FROM source_file")
            ).scalar_one()
            is None
        )
        assert "user_fund_access" in inspect(connection).get_table_names()
        connection.execute(
            text("UPDATE user_account SET fund_access_mode = 'selected'")
        )
        connection.execute(
            text("INSERT INTO user_fund_access (user_id, fund_id) VALUES (1, 1)")
        )
        connection.execute(text("UPDATE source_file SET recognized_fund_id = 1"))
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(text("UPDATE user_account SET fund_access_mode = 'unknown'"))
    with engine.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text("INSERT INTO user_fund_access (user_id, fund_id) VALUES (1, 1)")
        )
    with pytest.raises(RuntimeError, match="scoped accounts"):
        command.downgrade(config, "0011_benchmark_daily")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT fund_access_mode FROM user_account")
            ).scalar_one()
            == "selected"
        )
        assert (
            connection.execute(
                text("SELECT fund_id FROM user_fund_access")
            ).scalar_one()
            == 1
        )
    engine.dispose()
