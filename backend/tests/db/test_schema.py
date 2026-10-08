from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Float, Numeric, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base
from app.db.models import (
    AccountSubjectDaily,
    Fund,
    FundDailySnapshot,
    PositionDaily,
    ValuationVersion,
)
from app.db.session import create_engine


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _fund(session: Session, name: str = "测试产品") -> Fund:
    fund = Fund(standard_name=name, product_code=None)
    session.add(fund)
    session.flush()
    return fund


def _version(
    fund: Fund,
    *,
    version_no: int,
    status: str = "pending_review",
    valuation_date: date = date(2026, 8, 25),
) -> ValuationVersion:
    return ValuationVersion(
        fund_id=fund.id,
        valuation_date=valuation_date,
        version_no=version_no,
        status=status,
    )


def test_metadata_creates_all_core_tables(session: Session) -> None:
    expected_tables = {
        "fund",
        "fund_alias",
        "share_class",
        "subject_mapping",
        "parser_rule_set",
        "source_message",
        "source_file",
        "import_batch",
        "import_batch_file",
        "background_job",
        "valuation_version",
        "validation_result",
        "field_provenance",
        "fund_daily_snapshot",
        "share_class_daily_snapshot",
        "account_subject_daily",
        "position_daily",
        "analysis_run",
        "fund_metric_daily",
        "company_metric_daily",
        "risk_rule",
        "risk_event",
        "user_account",
        "user_session",
        "audit_log",
    }

    assert expected_tables.issubset(Base.metadata.tables)


def test_source_file_hash_is_required_and_unique(session: Session) -> None:
    from app.db.models import SourceFile

    session.add_all(
        [
            SourceFile(
                original_filename="first.xlsx",
                file_hash="a" * 64,
                file_size=10,
                file_extension=".xlsx",
                source_type="upload",
                object_name="object-1",
            ),
            SourceFile(
                original_filename="second.xlsx",
                file_hash="a" * 64,
                file_size=20,
                file_extension=".xlsx",
                source_type="upload",
                object_name="object-2",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_source_file_hash_must_be_sha256_length(session: Session) -> None:
    from app.db.models import SourceFile

    session.add(
        SourceFile(
            original_filename="invalid.xlsx",
            file_hash="short",
            file_size=10,
            file_extension=".xlsx",
            source_type="upload",
            object_name="object-invalid",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_fund_and_valuation_version_allow_multiple_pending_review_versions(
    session: Session,
) -> None:
    fund = _fund(session)
    session.add_all([_version(fund, version_no=1), _version(fund, version_no=2)])

    session.commit()

    versions = session.scalars(select(ValuationVersion)).all()
    assert [version.version_no for version in versions] == [1, 2]


def test_same_fund_date_and_version_number_is_unique(session: Session) -> None:
    fund = _fund(session)
    session.add_all([_version(fund, version_no=1), _version(fund, version_no=1)])

    with pytest.raises(IntegrityError):
        session.commit()


def test_same_fund_date_allows_one_published_and_multiple_pending_versions(
    session: Session,
) -> None:
    fund = _fund(session)
    session.add_all(
        [
            _version(fund, version_no=1, status="published"),
            _version(fund, version_no=2),
            _version(fund, version_no=3),
        ]
    )

    session.commit()


def test_same_fund_date_rejects_second_published_version(session: Session) -> None:
    fund = _fund(session)
    session.add_all(
        [
            _version(fund, version_no=1, status="published"),
            _version(fund, version_no=2, status="published"),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_fund_daily_snapshot_is_unique_per_valuation_version(
    session: Session,
) -> None:
    fund = _fund(session)
    version = _version(fund, version_no=1)
    session.add(version)
    session.flush()
    session.add_all(
        [
            FundDailySnapshot(valuation_version_id=version.id),
            FundDailySnapshot(valuation_version_id=version.id),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_core_foreign_key_relationships_work(session: Session) -> None:
    fund = _fund(session)
    version = _version(fund, version_no=1)
    session.add(version)
    session.flush()

    snapshot = FundDailySnapshot(valuation_version_id=version.id)
    session.add(snapshot)
    session.commit()

    assert version.fund is fund
    assert snapshot.valuation_version is version
    assert session.get(FundDailySnapshot, snapshot.id) is snapshot


def test_financial_columns_use_numeric_not_float() -> None:
    numeric_columns = {
        "total_assets": FundDailySnapshot.__table__.c.total_assets,
        "net_asset_value": FundDailySnapshot.__table__.c.net_asset_value,
        "unit_nav": FundDailySnapshot.__table__.c.unit_nav,
        "daily_return": FundDailySnapshot.__table__.c.daily_return,
    }

    for column in numeric_columns.values():
        assert isinstance(column.type, Numeric)
        assert not isinstance(column.type, Float)


def test_position_and_subject_source_columns_are_nullable() -> None:
    expected_nullable_columns = (
        AccountSubjectDaily.__table__.c.source_worksheet,
        AccountSubjectDaily.__table__.c.source_row,
        PositionDaily.__table__.c.original_subject_code,
        PositionDaily.__table__.c.source_worksheet,
        PositionDaily.__table__.c.source_row,
        PositionDaily.__table__.c.market,
        PositionDaily.__table__.c.account,
    )

    assert all(column.nullable for column in expected_nullable_columns)


def test_alembic_upgrade_creates_initial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "development")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    migrated_engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        table_names = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
        )
        analysis_columns = {
            column["name"] for column in inspector.get_columns("analysis_run")
        }
        analysis_foreign_keys = inspector.get_foreign_keys("analysis_run")
        analysis_indexes = {
            index["name"] for index in inspector.get_indexes("analysis_run")
        }
    assert set(Base.metadata.tables).issubset(table_names)
    assert "trigger_version_id" in analysis_columns
    assert any(
        foreign_key["constrained_columns"] == ["trigger_version_id"]
        for foreign_key in analysis_foreign_keys
    )
    assert "ix_analysis_run_trigger_version_id" in analysis_indexes


def test_initial_migration_downgrade_preserves_existing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "non-destructive-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "development")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    command.upgrade(config, "head")

    migrated_engine = create_engine(database_url)
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fund (standard_name, status) VALUES ('保留产品', 'active')"
            )
        )

    command.downgrade(config, "base")
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT standard_name FROM fund")).scalar_one()
            == "保留产品"
        )

    command.upgrade(config, "head")


def test_job_lease_index_migration_is_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "job-lease-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "development")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    command.upgrade(config, "0003_import_job_lease")
    migrated_engine = create_engine(database_url)
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        assert "lease_token" in {
            column["name"] for column in inspector.get_columns("background_job")
        }
        assert "ix_background_job_claim" not in {
            index["name"] for index in inspector.get_indexes("background_job")
        }

    command.upgrade(config, "head")
    with migrated_engine.connect() as connection:
        assert "ix_background_job_claim" in {
            index["name"] for index in inspect(connection).get_indexes("background_job")
        }

    command.downgrade(config, "0003_import_job_lease")
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        assert "ix_background_job_claim" not in {
            index["name"] for index in inspector.get_indexes("background_job")
        }
        assert "lease_token" in {
            column["name"] for column in inspector.get_columns("background_job")
        }
    migrated_engine.dispose()


def test_position_source_migration_is_reversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "position-source-migration.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "development")
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    subject_source_columns = {"source_worksheet", "source_row"}
    position_source_columns = {
        "original_subject_code",
        "source_worksheet",
        "source_row",
    }

    command.upgrade(config, "head")
    migrated_engine = create_engine(database_url)
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        assert subject_source_columns.issubset(
            {
                column["name"]
                for column in inspector.get_columns("account_subject_daily")
            }
        )
        assert position_source_columns.issubset(
            {column["name"] for column in inspector.get_columns("position_daily")}
        )

    command.downgrade(config, "0005_analysis_trigger_version")
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        assert subject_source_columns.isdisjoint(
            {
                column["name"]
                for column in inspector.get_columns("account_subject_daily")
            }
        )
        assert position_source_columns.isdisjoint(
            {column["name"] for column in inspector.get_columns("position_daily")}
        )

    command.upgrade(config, "head")
    with migrated_engine.connect() as connection:
        inspector = inspect(connection)
        assert subject_source_columns.issubset(
            {
                column["name"]
                for column in inspector.get_columns("account_subject_daily")
            }
        )
        assert position_source_columns.issubset(
            {column["name"] for column in inspector.get_columns("position_daily")}
        )
    migrated_engine.dispose()
