"""Audited, repeatable production catalog initialization command."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.bootstrap_config import (
    AliasConfig,
    BootstrapConfig,
    BootstrapError,
    ProductConfig,
    RiskRuleConfig,
    ShareClassConfig,
    SubjectMappingConfig,
    load_config,
    validate_config,
)
from app.db.models import (
    Fund,
    FundAlias,
    RiskRule,
    ShareClass,
    SubjectMapping,
    SystemState,
)
from app.migration.inventory import (
    ACTION_IMPORT,
    ACTION_IMPORT_GZ_ONLY,
    ACTION_NEEDS_REVIEW,
    ACTION_SKIP_DUPLICATE,
    ACTION_SKIP_NON_VALUATION,
)
from app.migration.manifest import load_manifest

BOOTSTRAP_FINGERPRINT_KEY = "_bootstrap_fingerprint"
_CATALOG_TABLES = (Fund, FundAlias, ShareClass, SubjectMapping, RiskRule)
_REQUIRED_TABLE_NAMES = {
    "fund",
    "fund_alias",
    "share_class",
    "subject_mapping",
    "risk_rule",
    "system_state",
    "audit_log",
}


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    dry_run: bool
    idempotent: bool
    created: dict[str, int]
    preflight: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "idempotent": self.idempotent,
            "created": self.created,
            "preflight": self.preflight,
        }


def run_bootstrap(
    session: Session,
    config: BootstrapConfig | dict[str, object],
    *,
    dry_run: bool = False,
    allow_existing: bool = False,
) -> BootstrapResult:
    """Run preflight and apply catalog data atomically when not in dry-run mode."""

    normalized = (
        config if isinstance(config, BootstrapConfig) else validate_config(config)
    )
    preflight = _run_preflight(session, normalized)
    fingerprint = normalized.fingerprint()
    state = session.get(SystemState, 1)
    if not dry_run:
        state = _lock_state(session, state)
    previous_fingerprint = _bootstrap_fingerprint(state)
    catalog_has_data = _catalog_has_data(session)
    if catalog_has_data and previous_fingerprint != fingerprint and not allow_existing:
        session.rollback()
        raise BootstrapError(
            "business catalog is not empty; rerun with --allow-existing after review"
        )

    created = _empty_counts()
    if dry_run:
        return BootstrapResult(
            dry_run=True,
            idempotent=previous_fingerprint == fingerprint and catalog_has_data,
            created=_planned_counts(session, normalized),
            preflight=preflight,
        )

    try:
        _apply_config(session, normalized, created)
        state = _ensure_state(session, state)
        settings = dict(state.settings) if isinstance(state.settings, dict) else {}
        settings[BOOTSTRAP_FINGERPRINT_KEY] = fingerprint
        is_idempotent = previous_fingerprint == fingerprint and not any(
            created.values()
        )
        if not is_idempotent:
            state.settings = settings
            AuthService(session).record_audit(
                action="bootstrap.completed",
                resource_type="bootstrap",
                resource_id=fingerprint[:16],
                summary={
                    "config_version": normalized.version,
                    "created": dict(created),
                    "allow_existing": allow_existing,
                },
            )
        session.commit()
    except (BootstrapError, SQLAlchemyError):
        session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - keep the CLI error boundary safe
        session.rollback()
        raise BootstrapError(f"bootstrap failed: {type(exc).__name__}") from None

    return BootstrapResult(
        dry_run=False,
        idempotent=previous_fingerprint == fingerprint and not any(created.values()),
        created=created,
        preflight=preflight,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.bootstrap",
        description="Validate and initialize production catalog data from JSON.",
    )
    parser.add_argument("--config", required=True, help="bootstrap JSON configuration")
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and preflight without writing"
    )
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="allow merging into an existing business catalog after review",
    )
    return parser


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    output = stdout or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        from app.config import get_settings
        from app.db.session import create_engine

        config = load_config(Path(args.config))
        engine = create_engine(get_settings().database_url)
        try:
            with Session(engine) as session:
                result = run_bootstrap(
                    session,
                    config,
                    dry_run=args.dry_run,
                    allow_existing=args.allow_existing,
                )
        finally:
            engine.dispose()
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2), file=output)
        return 0
    except (BootstrapError, OSError, SQLAlchemyError) as exc:
        print(f"错误：{exc}", file=output)
        return 2


def _run_preflight(session: Session, config: BootstrapConfig) -> dict[str, object]:
    preflight = config.preflight
    try:
        if not preflight.storage_root.is_dir():
            raise BootstrapError("storage root is not an existing directory")
        if not os.access(preflight.storage_root, os.R_OK | os.W_OK):
            raise BootstrapError("storage root is not readable and writable")
    except BootstrapError:
        raise
    except OSError:
        raise BootstrapError("storage root preflight failed") from None

    try:
        connection = session.connection()
        connection.execute(text("SELECT 1"))
        tables = set(inspect(connection).get_table_names())
        if not _REQUIRED_TABLE_NAMES.issubset(tables):
            raise BootstrapError("database schema preflight failed")
    except (SQLAlchemyError, OSError):
        raise BootstrapError("database preflight failed") from None

    try:
        manifest = load_manifest(preflight.migration_manifest)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise BootstrapError("migration manifest preflight failed") from None
    statuses: dict[str, int] = {}
    manifest_products: set[str] = set()
    for entry in manifest.entries:
        _validate_manifest_entry(
            entry.action, entry.status, entry.attempts, entry.size_bytes
        )
        statuses[entry.status] = statuses.get(entry.status, 0) + 1
        if entry.product is None:
            continue
        if not isinstance(entry.product, str):
            raise BootstrapError("migration manifest preflight failed")
        product = entry.product.strip()
        if product:
            manifest_products.add(product)
    known_products = {
        _normalize(product.standard_name) for product in config.products
    } | {
        _normalize(alias.alias)
        for product in config.products
        for alias in product.aliases
    }
    unresolved_products = sorted(
        product
        for product in manifest_products
        if _normalize(product) not in known_products
    )
    if unresolved_products:
        raise BootstrapError("migration manifest contains unconfigured products")
    return {
        "database": "ok",
        "storage_root": "ok",
        "migration_manifest": {
            "status": "ok",
            "root_name": manifest.root_name,
            "entry_count": len(manifest.entries),
            "status_counts": statuses,
        },
        "catalog_readiness": {
            "configured_product_count": len(config.products),
            "configured_alias_count": sum(
                len(product.aliases) for product in config.products
            ),
            "configured_subject_mapping_count": len(config.subject_mappings),
            "manifest_product_count": len(manifest_products),
            "unresolved_manifest_products": unresolved_products,
            "alias_resolution_status": "manifest_product_labels_only",
            "subject_mapping_status": "sample_validation_required",
        },
    }


def _validate_manifest_entry(
    action: str, status: str, attempts: int, size_bytes: int
) -> None:
    valid_actions = {
        ACTION_IMPORT,
        ACTION_IMPORT_GZ_ONLY,
        ACTION_SKIP_DUPLICATE,
        ACTION_SKIP_NON_VALUATION,
        ACTION_NEEDS_REVIEW,
    }
    valid_statuses = {"pending", "uploaded", "failed", "skipped", "needs_review"}
    if action not in valid_actions or status not in valid_statuses:
        raise BootstrapError("migration manifest contains invalid entry state")
    expected_statuses = (
        {"pending", "uploaded", "failed"}
        if action in {ACTION_IMPORT, ACTION_IMPORT_GZ_ONLY}
        else {"needs_review"}
        if action == ACTION_NEEDS_REVIEW
        else {"skipped"}
    )
    if status not in expected_statuses or attempts < 0 or size_bytes < 0:
        raise BootstrapError("migration manifest contains invalid entry state")


def _apply_config(
    session: Session, config: BootstrapConfig, created: dict[str, int]
) -> None:
    for product in config.products:
        fund, did_create = _ensure_fund(session, product)
        if did_create:
            created["funds"] += 1
        for alias_config in product.aliases:
            _, alias_created = _ensure_alias(session, fund, alias_config)
            if alias_created:
                created["aliases"] += 1
        for share_config in product.share_classes:
            _, share_created = _ensure_share_class(session, fund, share_config)
            if share_created:
                created["share_classes"] += 1

    for mapping_config in config.subject_mappings:
        _, mapping_created = _ensure_mapping(session, mapping_config)
        if mapping_created:
            created["subject_mappings"] += 1
    for rule_config in config.risk_rules:
        _, rule_created = _ensure_risk_rule(session, rule_config)
        if rule_created:
            created["risk_rules"] += 1
    if config.system_settings:
        changed = _ensure_system_settings(session, config.system_settings)
        created["system_settings"] = len(changed)
        if changed:
            AuthService(session).record_audit(
                action="bootstrap.system_settings_updated",
                resource_type="system_settings",
                resource_id="1",
                summary={"changed_keys": sorted(changed)},
            )


def _ensure_fund(session: Session, config: ProductConfig) -> tuple[Fund, bool]:
    normalized_name = _normalize(config.standard_name)
    fund = next(
        (
            item
            for item in session.scalars(select(Fund)).all()
            if _normalize(item.standard_name) == normalized_name
        ),
        None,
    )
    if fund is not None:
        _assert_same(
            fund.product_code,
            config.product_code,
            fund.establishment_date,
            config.establishment_date,
            fund.strategy,
            config.strategy,
            fund.manager,
            config.manager,
            fund.notes,
            config.notes,
            fund.status,
            config.status,
            "fund identity conflicts with existing record",
        )
        return fund, False
    if any(
        _normalize(item.alias) == normalized_name
        for item in session.scalars(select(FundAlias))
    ):
        raise BootstrapError("fund name conflicts with an existing alias")
    if config.product_code is not None and any(
        item.product_code is not None
        and _normalize(item.product_code) == _normalize(config.product_code)
        for item in session.scalars(select(Fund))
    ):
        raise BootstrapError("product code conflicts with an existing record")
    fund = Fund(
        standard_name=config.standard_name,
        product_code=config.product_code,
        establishment_date=config.establishment_date,
        strategy=config.strategy,
        manager=config.manager,
        notes=config.notes,
        status=config.status,
    )
    session.add(fund)
    session.flush()
    _audit_created(session, "fund", fund.id, "bootstrap.fund_created")
    return fund, True


def _ensure_alias(
    session: Session, fund: Fund, config: AliasConfig
) -> tuple[FundAlias, bool]:
    normalized_alias = _normalize(config.alias)
    if _normalize(fund.standard_name) == normalized_alias:
        raise BootstrapError("alias must differ from fund name")
    if any(
        _normalize(item.standard_name) == normalized_alias
        for item in session.scalars(select(Fund).where(Fund.id != fund.id))
    ):
        raise BootstrapError("alias conflicts with an existing fund")
    if any(
        _normalize(item.alias) == normalized_alias and item.fund_id != fund.id
        for item in session.scalars(select(FundAlias))
    ):
        raise BootstrapError("alias conflicts with an existing alias")
    alias = next(
        (
            item
            for item in session.scalars(
                select(FundAlias).where(FundAlias.fund_id == fund.id)
            ).all()
            if _normalize(item.alias) == normalized_alias
        ),
        None,
    )
    if alias is not None:
        _assert_same(
            alias.source_location,
            config.source_location,
            alias.match_priority,
            config.match_priority,
            alias.valid_from,
            config.valid_from,
            alias.valid_to,
            config.valid_to,
            "alias conflicts with existing record",
        )
        return alias, False
    alias = FundAlias(
        fund_id=fund.id,
        alias=config.alias,
        source_location=config.source_location,
        match_priority=config.match_priority,
        valid_from=config.valid_from,
        valid_to=config.valid_to,
    )
    session.add(alias)
    session.flush()
    _audit_created(session, "fund_alias", alias.id, "bootstrap.alias_created")
    return alias, True


def _ensure_share_class(
    session: Session, fund: Fund, config: ShareClassConfig
) -> tuple[ShareClass, bool]:
    normalized_code = _normalize(config.share_code)
    share_class = next(
        (
            item
            for item in session.scalars(
                select(ShareClass).where(ShareClass.fund_id == fund.id)
            ).all()
            if _normalize(item.share_code) == normalized_code
        ),
        None,
    )
    if share_class is not None:
        _assert_same(
            share_class.share_name,
            config.share_name,
            share_class.enabled_from,
            config.enabled_from,
            share_class.disabled_from,
            config.disabled_from,
            share_class.notes,
            config.notes,
            "share class conflicts with existing record",
        )
        return share_class, False
    share_class = ShareClass(
        fund_id=fund.id,
        share_code=config.share_code,
        share_name=config.share_name,
        enabled_from=config.enabled_from,
        disabled_from=config.disabled_from,
        notes=config.notes,
    )
    session.add(share_class)
    session.flush()
    _audit_created(
        session, "share_class", share_class.id, "bootstrap.share_class_created"
    )
    return share_class, True


def _ensure_mapping(
    session: Session, config: SubjectMappingConfig
) -> tuple[SubjectMapping, bool]:
    mappings = session.scalars(select(SubjectMapping)).all()
    mapping = next(
        (
            item
            for item in mappings
            if item.subject_code_or_prefix == config.subject_code_or_prefix
            and item.raw_name_pattern == config.raw_name_pattern
            and item.rule_version == config.rule_version
        ),
        None,
    )
    if mapping is not None:
        _assert_same(
            mapping.is_leaf,
            config.is_leaf,
            mapping.include_in_holdings,
            config.include_in_holdings,
            mapping.valid_from,
            config.valid_from,
            mapping.valid_to,
            config.valid_to,
            mapping.standard_category,
            config.standard_category,
            mapping.status,
            config.status,
            "subject mapping conflicts with existing record",
        )
        return mapping, False
    mapping = SubjectMapping(
        subject_code_or_prefix=config.subject_code_or_prefix,
        raw_name_pattern=config.raw_name_pattern,
        standard_category=config.standard_category,
        is_leaf=config.is_leaf,
        include_in_holdings=config.include_in_holdings,
        valid_from=config.valid_from,
        valid_to=config.valid_to,
        rule_version=config.rule_version,
        status=config.status,
    )
    session.add(mapping)
    session.flush()
    _audit_created(
        session, "subject_mapping", mapping.id, "bootstrap.subject_mapping_created"
    )
    return mapping, True


def _ensure_risk_rule(
    session: Session, config: RiskRuleConfig
) -> tuple[RiskRule, bool]:
    rule = session.scalar(
        select(RiskRule).where(
            RiskRule.rule_code == config.rule_code,
            RiskRule.version == config.version,
        )
    )
    if rule is not None:
        _assert_same(
            rule.rule_type,
            config.rule_type,
            rule.scope,
            config.scope,
            rule.threshold,
            config.threshold,
            rule.severity,
            config.severity,
            rule.valid_from,
            config.valid_from,
            rule.valid_to,
            config.valid_to,
            rule.enabled,
            config.enabled,
            "risk rule conflicts with existing record",
        )
        return rule, False
    rule = RiskRule(
        rule_code=config.rule_code,
        rule_type=config.rule_type,
        scope=config.scope,
        threshold=config.threshold,
        severity=config.severity,
        valid_from=config.valid_from,
        valid_to=config.valid_to,
        version=config.version,
        enabled=config.enabled,
    )
    session.add(rule)
    session.flush()
    _audit_created(session, "risk_rule", rule.id, "bootstrap.risk_rule_created")
    return rule, True


def _ensure_system_settings(session: Session, values: dict[str, object]) -> list[str]:
    state = _ensure_state(session, session.get(SystemState, 1))
    current = dict(state.settings) if isinstance(state.settings, dict) else {}
    changed = [key for key, value in values.items() if current.get(key) != value]
    if changed:
        state.settings = {**current, **values}
        session.flush()
    return changed


def _planned_counts(session: Session, config: BootstrapConfig) -> dict[str, int]:
    counts = _empty_counts()
    with session.begin_nested() as savepoint:
        _apply_config(session, config, counts)
        savepoint.rollback()
    return counts


def _catalog_has_data(session: Session) -> bool:
    return any(
        session.scalar(select(table.id).limit(1)) is not None
        for table in _CATALOG_TABLES
    )


def _bootstrap_fingerprint(state: SystemState | None) -> str | None:
    if state is None or not isinstance(state.settings, dict):
        return None
    value = state.settings.get(BOOTSTRAP_FINGERPRINT_KEY)
    return value if isinstance(value, str) else None


def _ensure_state(session: Session, state: SystemState | None) -> SystemState:
    if state is None:
        state = next(
            (
                item
                for item in session.new
                if isinstance(item, SystemState) and item.id == 1
            ),
            None,
        )
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    return state


def _lock_state(session: Session, state: SystemState | None) -> SystemState:
    """Serialize formal bootstrap runs on the singleton state row."""

    locked = _ensure_state(session, state)
    if session.get_bind().dialect.name != "postgresql":
        return locked
    refreshed = session.scalar(
        select(SystemState).where(SystemState.id == 1).with_for_update()
    )
    if refreshed is None:
        raise BootstrapError("bootstrap state lock failed")
    return refreshed


def _audit_created(
    session: Session, resource_type: str, resource_id: int, action: str
) -> None:
    AuthService(session).record_audit(
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
    )


def _assert_same(*values: object) -> None:
    message = values[-1]
    pairs = values[:-1]
    if len(pairs) % 2 != 0:
        raise BootstrapError("bootstrap comparison failed")
    if any(left != right for left, right in zip(pairs[::2], pairs[1::2], strict=True)):
        raise BootstrapError(str(message))


def _normalize(value: str | None) -> str:
    return (value or "").strip().casefold()


def _empty_counts() -> dict[str, int]:
    return {
        "funds": 0,
        "aliases": 0,
        "share_classes": 0,
        "subject_mappings": 0,
        "risk_rules": 0,
        "system_settings": 0,
    }


__all__ = [
    "BootstrapConfig",
    "BootstrapError",
    "BootstrapResult",
    "load_config",
    "main",
    "run_bootstrap",
    "validate_config",
]


if __name__ == "__main__":
    raise SystemExit(main())
