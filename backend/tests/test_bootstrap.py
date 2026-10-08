from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bootstrap import (
    BootstrapError,
    load_config,
    main,
    run_bootstrap,
    validate_config,
)
from app.db.base import Base
from app.db.models import (
    AuditLog,
    Fund,
    FundAlias,
    RiskRule,
    ShareClass,
    SubjectMapping,
)
from app.db.session import create_engine


@pytest.fixture()
def session_and_paths(tmp_path: Path) -> Iterator[tuple[Session, Path, Path]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    storage_root = tmp_path / "source-files"
    storage_root.mkdir()
    manifest_path = tmp_path / "migration-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "root_name": "historical-source",
                "inventory_fingerprint": "test-fingerprint",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    with Session(engine) as session:
        yield session, storage_root, manifest_path
    engine.dispose()


def _config(storage_root: Path, manifest_path: Path) -> dict[str, object]:
    return {
        "version": 1,
        "preflight": {
            "storage_root": str(storage_root),
            "migration_manifest": str(manifest_path),
        },
        "products": [
            {
                "standard_name": "梦溪一号",
                "product_code": "M001",
                "establishment_date": "2024-06-24",
                "strategy": "测试策略",
                "aliases": [
                    {
                        "alias": "梦溪一号估值表",
                        "source_location": "primary",
                        "match_priority": 10,
                    }
                ],
                "share_classes": [
                    {"share_code": "A", "share_name": "A类"},
                ],
            }
        ],
        "subject_mappings": [
            {
                "subject_code_or_prefix": "1101",
                "standard_category": "银行存款",
                "rule_version": "1",
                "is_leaf": True,
                "include_in_holdings": False,
            }
        ],
        "risk_rules": [
            {
                "rule_code": "daily_return_limit",
                "rule_type": "daily_return",
                "scope": "all",
                "threshold": "-0.05",
                "severity": "warning",
                "version": "1",
                "enabled": True,
            }
        ],
        "system_settings": {
            "mail_sync_schedule": {"mode": "interval", "interval_minutes": 15},
            "timezone": "Asia/Shanghai",
        },
    }


def test_dry_run_validates_without_writing(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths

    result = run_bootstrap(
        session,
        _config(storage_root, manifest_path),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.created == {
        "funds": 1,
        "aliases": 1,
        "share_classes": 1,
        "subject_mappings": 1,
        "risk_rules": 1,
        "system_settings": 2,
    }
    assert session.scalar(select(Fund.id)) is None
    assert session.scalar(select(AuditLog.id)) is None


def test_bootstrap_writes_catalog_settings_and_audits(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths

    result = run_bootstrap(session, _config(storage_root, manifest_path))

    assert result.dry_run is False
    assert session.scalar(select(Fund.standard_name)) == "梦溪一号"
    assert session.scalar(select(FundAlias.alias)) == "梦溪一号估值表"
    assert session.scalar(select(ShareClass.share_code)) == "A"
    assert session.scalar(select(SubjectMapping.standard_category)) == "银行存款"
    assert session.scalar(select(RiskRule.rule_code)) == "daily_return_limit"
    actions = set(session.scalars(select(AuditLog.action)).all())
    assert "bootstrap.fund_created" in actions
    assert "bootstrap.completed" in actions


def test_product_status_is_initialized(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    config = _config(storage_root, manifest_path)
    config["products"][0]["status"] = "inactive"  # type: ignore[index]

    run_bootstrap(session, config)

    assert session.scalar(select(Fund.status)) == "inactive"


def test_repeated_bootstrap_is_idempotent(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    config = _config(storage_root, manifest_path)

    first = run_bootstrap(session, config)
    audit_count = session.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()))
    second = run_bootstrap(session, config)

    assert first.created["funds"] == 1
    assert second.idempotent is True
    assert second.created["funds"] == 0
    assert session.scalar(select(Fund.id)) == 1
    assert (
        session.scalar(select(AuditLog.id).order_by(AuditLog.id.desc())) == audit_count
    )


def test_existing_business_data_requires_explicit_override(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    session.add(Fund(standard_name="已存在产品", product_code="OLD"))
    session.commit()

    with pytest.raises(BootstrapError, match="business catalog is not empty"):
        run_bootstrap(session, _config(storage_root, manifest_path))

    result = run_bootstrap(
        session,
        _config(storage_root, manifest_path),
        allow_existing=True,
    )
    assert result.idempotent is False
    assert (
        session.scalar(select(Fund).where(Fund.standard_name == "梦溪一号")) is not None
    )


def test_sensitive_configuration_fields_are_rejected(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    _, storage_root, manifest_path = session_and_paths
    config = _config(storage_root, manifest_path)
    config["system_settings"] = {"password": "should-not-be-read"}

    with pytest.raises(BootstrapError, match="forbidden configuration field"):
        validate_config(config)


def test_invalid_manifest_fails_preflight_before_writing(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    manifest_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(BootstrapError, match="migration manifest"):
        run_bootstrap(session, _config(storage_root, manifest_path))

    assert session.scalar(select(Fund.id)) is None


def test_manifest_with_non_text_product_fails_preflight_safely(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "root_name": "historical-source",
                "inventory_fingerprint": "test-fingerprint",
                "entries": [
                    {
                        "rel_path": "invalid-product.xls",
                        "product": 123,
                        "size_bytes": 1,
                        "action": "import",
                        "status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BootstrapError, match="migration manifest preflight failed"):
        run_bootstrap(session, _config(storage_root, manifest_path), dry_run=True)

    assert session.scalar(select(Fund.id)) is None


def test_load_config_reports_json_shape_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps([{"password": "secret-value"}]), encoding="utf-8")

    with pytest.raises(BootstrapError) as error:
        load_config(path)

    assert "secret-value" not in str(error.value)
    assert "object" in str(error.value)


def test_dates_and_decimal_are_normalized(tmp_path: Path) -> None:
    storage_root = tmp_path / "source-files"
    storage_root.mkdir()
    config = validate_config(
        {
            "version": 1,
            "preflight": {
                "storage_root": str(storage_root),
                "migration_manifest": str(tmp_path / "migration-manifest.json"),
            },
            "products": [
                {
                    "standard_name": "产品",
                    "product_code": "P001",
                    "establishment_date": "2024-01-02",
                    "aliases": [{"alias": "产品别名"}],
                }
            ],
            "risk_rules": [
                {
                    "rule_code": "r1",
                    "rule_type": "daily_return",
                    "threshold": "-0.1",
                    "version": "1",
                }
            ],
        }
    )

    assert config.products[0].establishment_date == date(2024, 1, 2)
    assert str(config.risk_rules[0].threshold) == "-0.1"


def test_unsupported_config_version_is_rejected(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    _, storage_root, manifest_path = session_and_paths
    config = _config(storage_root, manifest_path)
    config["version"] = 2

    with pytest.raises(BootstrapError, match="invalid bootstrap configuration"):
        validate_config(config)


def test_manifest_product_must_match_a_configured_name_or_alias(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "root_name": "historical-source",
                "inventory_fingerprint": "test-fingerprint",
                "entries": [
                    {
                        "rel_path": "unknown.xls",
                        "product": "未配置产品",
                        "size_bytes": 1,
                        "source_zone": "primary",
                        "file_type": "xls",
                        "is_valuation": True,
                        "action": "import",
                        "note": "",
                        "error_message": "",
                        "status": "pending",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(BootstrapError, match="unconfigured products"):
        run_bootstrap(session, _config(storage_root, manifest_path), dry_run=True)

    assert session.scalar(select(Fund.id)) is None


def test_invalid_manifest_entry_state_is_rejected(
    session_and_paths: tuple[Session, Path, Path],
) -> None:
    session, storage_root, manifest_path = session_and_paths
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "root_name": "historical-source",
                "inventory_fingerprint": "test-fingerprint",
                "entries": [
                    {
                        "rel_path": "invalid.xls",
                        "product": "梦溪一号",
                        "size_bytes": 1,
                        "action": "import",
                        "status": "bogus",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BootstrapError, match="invalid entry state"):
        run_bootstrap(session, _config(storage_root, manifest_path), dry_run=True)


def test_cli_dry_run_uses_configured_database_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "bootstrap.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    storage_root = tmp_path / "source-files"
    storage_root.mkdir()
    manifest_path = tmp_path / "migration-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "root_name": "historical-source",
                "inventory_fingerprint": "test-fingerprint",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "bootstrap.json"
    config_path.write_text(
        json.dumps(_config(storage_root, manifest_path), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("APP_ENV", "development")
    output = StringIO()

    exit_code = main(["--config", str(config_path), "--dry-run"], stdout=output)

    assert exit_code == 0
    assert '"dry_run": true' in output.getvalue()
    with Session(create_engine(database_url)) as session:
        assert session.scalar(select(Fund.id)) is None
