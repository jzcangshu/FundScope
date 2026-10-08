"""Background processing seam from immutable files to valuation versions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import AuditResult, ImportBatchStatus, ValuationStatus
from app.db.models import (
    AccountSubjectDaily,
    AuditLog,
    FieldProvenance,
    Fund,
    FundAlias,
    FundDailySnapshot,
    ImportBatch,
    ImportBatchFile,
    PositionDaily,
    ShareClass,
    ShareClassDailySnapshot,
    SourceFile,
    SubjectMapping,
    ValuationVersion,
)
from app.parser import ValuationParser
from app.parser.interface import ParsedShareClass, ParsedSubject, ParsedValuation
from app.parser.nav_history import NavHistoryEntry, parse_nav_history
from app.parser.normalizers import text
from app.parser.valuation_parser import ParseError
from app.publishing import PublishingService
from app.validation import ValidationService
from app.validation.rules import ToleranceConfig

from .access import ImportAccessError, actor_fund_ids, authorized_workbook_fund
from .storage import resolve_in_root


@dataclass(frozen=True, slots=True)
class BatchProcessResult:
    batch_id: int
    processed_files: int
    duplicate_files: int
    non_valuation_files: int
    failed_files: int
    review_files: int
    published_files: int
    analysis_run_ids: tuple[int, ...]
    created_versions: tuple[int, ...]


class BatchProcessingError(RuntimeError):
    """Stable processing error for the job runner."""


def process_import_batch(
    session: Session,
    batch_id: int,
    settings: Settings,
) -> BatchProcessResult:
    """Parse one completed batch and persist only candidate versions.

    The operation is idempotent by ``source_file_id``.  It never changes an
    existing valuation version and never promotes an unresolved product/date.
    """

    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise BatchProcessingError("import_batch_not_found")
    if batch.status not in {
        ImportBatchStatus.QUEUED,
        ImportBatchStatus.PROCESSING,
        ImportBatchStatus.COMPLETED,
    }:
        raise BatchProcessingError("import_batch_not_queued")

    batch.status = ImportBatchStatus.PROCESSING
    batch.started_at = datetime.now(UTC)
    session.flush()

    aliases, fund_lookup = _load_product_identity(session)
    mappings = _subject_mappings(session)
    parser = ValuationParser(aliases)
    links = tuple(
        session.scalars(
            select(ImportBatchFile)
            .where(ImportBatchFile.batch_id == batch.id)
            .order_by(ImportBatchFile.id)
        ).all()
    )
    allowed_ids = actor_fund_ids(session, batch.created_by_user_id)
    if allowed_ids is not None:
        # Check the complete batch before creating any versions. A queued upload
        # cannot retain access after its creator's grants/account have changed.
        for link in links:
            source = session.get(SourceFile, link.source_file_id)
            if source is None:
                raise ImportAccessError("product_access_denied")
            path = resolve_in_root(
                Path(settings.source_storage_dir), source.object_name
            )
            resolved_id = authorized_workbook_fund(session, path, allowed_ids)
            existing_funds = set(
                session.scalars(
                    select(ValuationVersion.fund_id).where(
                        ValuationVersion.source_file_id == source.id
                    )
                )
            )
            if existing_funds - allowed_ids:
                raise ImportAccessError("product_access_denied")
            source.recognized_fund_id = resolved_id

    counters = {
        "processed": 0,
        "duplicates": 0,
        "non_valuation": 0,
        "failed": 0,
        "review": 0,
        "published": 0,
    }
    created_versions: list[int] = []
    auto_published_by_fund: dict[int, list[ValuationVersion]] = {}

    for link in links:
        source_file = session.get(SourceFile, link.source_file_id)
        if source_file is None:
            raise BatchProcessingError("source_file_not_found")
        existing = session.scalar(
            select(ValuationVersion.id).where(
                ValuationVersion.source_file_id == source_file.id
            )
        )
        if existing is not None:
            counters["duplicates"] += 1
            continue

        path = resolve_in_root(
            Path(settings.source_storage_dir), source_file.object_name
        )
        try:
            parsed = parser.parse(path)
        except ParseError as exc:
            # Not a valuation workbook — try the multi-date NAV history layout.
            try:
                nav_entries = parse_nav_history(path)
            except Exception:  # noqa: BLE001 - neither known template
                counters["non_valuation"] += 1
                _audit(
                    session,
                    action="import.non_valuation",
                    batch=batch,
                    source_file=source_file,
                    summary={"reason": str(exc)},
                )
                continue
            _process_nav_history_file(
                session,
                batch=batch,
                source_file=source_file,
                path=path,
                entries=nav_entries,
                aliases=aliases,
                fund_lookup=fund_lookup,
                counters=counters,
                created_versions=created_versions,
                auto_published_by_fund=auto_published_by_fund,
            )
            continue
        except Exception:  # noqa: BLE001 - isolate one malformed workbook
            counters["failed"] += 1
            _audit(
                session,
                action="import.file_failed",
                batch=batch,
                source_file=source_file,
                summary={"reason": "parser_failed"},
                result=AuditResult.FAILURE,
            )
            continue

        fund = _resolve_fund(session, parsed.product_name, lookup=fund_lookup)
        if fund is None or parsed.valuation_date is None:
            counters["review"] += 1
            _audit(
                session,
                action="import.review_required",
                batch=batch,
                source_file=source_file,
                summary={
                    "product": parsed.product_name,
                    "product_candidates": list(parsed.product_candidates),
                    "valuation_date": (
                        parsed.valuation_date.isoformat()
                        if parsed.valuation_date
                        else None
                    ),
                    "warnings": list(parsed.warnings),
                },
                result=AuditResult.FAILURE,
            )
            continue

        _recognize_source_fund(session, source_file, path, fund.id)
        version = _persist_parsed_version(
            session,
            fund,
            source_file,
            parsed,
            mappings=_active_mappings(mappings, parsed.valuation_date),
        )
        report = ValidationService(session).validate_version(version.id, parsed=parsed)
        if report.critical_count:
            counters["review"] += 1
        elif report.warning_count == 0:
            PublishingService(session).publish_version(
                version.id,
                actor_user_id=batch.created_by_user_id,
                actor_label="system:auto-import",
                reason="自动发布：导入校验通过",
                schedule_analysis=False,
            )
            auto_published_by_fund.setdefault(fund.id, []).append(version)
            counters["published"] += 1
        created_versions.append(version.id)
        counters["processed"] += 1

    analysis_run_ids: list[int] = []
    publisher = PublishingService(session)
    for fund_versions in auto_published_by_fund.values():
        dates = [version.valuation_date for version in fund_versions]
        latest = max(
            fund_versions, key=lambda version: (version.valuation_date, version.id)
        )
        analysis_run = publisher.queue_analysis_run(
            latest.id,
            start_date=min(dates),
            actor_user_id=batch.created_by_user_id,
            trigger_reason="import_batch_auto_published",
        )
        analysis_run_ids.append(analysis_run.id)

    batch.status = ImportBatchStatus.COMPLETED
    batch.ended_at = datetime.now(UTC)
    session.flush()
    return BatchProcessResult(
        batch_id=batch.id,
        processed_files=counters["processed"],
        duplicate_files=counters["duplicates"],
        non_valuation_files=counters["non_valuation"],
        failed_files=counters["failed"],
        review_files=counters["review"],
        published_files=counters["published"],
        analysis_run_ids=tuple(analysis_run_ids),
        created_versions=tuple(created_versions),
    )


def _load_product_identity(
    session: Session,
) -> tuple[dict[str, tuple[str, ...]], dict[str, Fund]]:
    aliases: dict[str, list[str]] = {}
    rows = list(
        session.execute(
            select(Fund, FundAlias.alias)
            .outerjoin(FundAlias, FundAlias.fund_id == Fund.id)
            .order_by(
                FundAlias.match_priority.desc().nulls_last(), Fund.id, FundAlias.id
            )
        )
    )
    lookup: dict[str, Fund] = {}
    for fund, alias in rows:
        aliases.setdefault(fund.standard_name, [])
        lookup.setdefault(fund.standard_name.strip().casefold(), fund)
        if alias is not None:
            aliases[fund.standard_name].append(alias)
    for fund, alias in rows:
        if alias is not None:
            lookup.setdefault(alias.strip().casefold(), fund)
    return (
        {standard_name: tuple(items) for standard_name, items in aliases.items()},
        lookup,
    )


def _product_aliases(session: Session) -> dict[str, tuple[str, ...]]:
    aliases, _ = _load_product_identity(session)
    return aliases


def _resolve_fund(
    session: Session,
    product_name: str | None,
    *,
    lookup: dict[str, Fund] | None = None,
) -> Fund | None:
    if not product_name:
        return None
    normalized = product_name.strip().casefold()
    if lookup is None:
        _, lookup = _load_product_identity(session)
    return lookup.get(normalized)


def _resolve_nav_fund(
    session: Session,
    entries: tuple[NavHistoryEntry, ...],
    path: Path,
    *,
    aliases: dict[str, tuple[str, ...]],
    fund_lookup: dict[str, Fund],
) -> Fund | None:
    """Bind one NAV history file to a fund by name, code, then filename."""

    for entry in entries:
        fund = fund_lookup.get(entry.product_name.strip().casefold())
        if fund is not None:
            return fund
    code_lookup = {
        fund.product_code.strip().upper(): fund
        for fund in session.scalars(select(Fund))
        if fund.product_code
    }
    for entry in entries:
        fund = code_lookup.get((entry.product_code or "").strip().upper())
        if fund is not None:
            return fund
    # Filename fallback: the longest catalog alias contained in the file name
    # wins, so 慧达二号 outranks 慧达 when both appear in one file name.
    best: Fund | None = None
    best_length = 0
    for standard_name, name_aliases in aliases.items():
        for alias in name_aliases:
            if alias and alias in path.stem and len(alias) > best_length:
                candidate = fund_lookup.get(standard_name.strip().casefold())
                if candidate is not None:
                    best = candidate
                    best_length = len(alias)
    return best


def _process_nav_history_file(
    session: Session,
    *,
    batch: ImportBatch,
    source_file: SourceFile,
    path: Path,
    entries: tuple[NavHistoryEntry, ...],
    aliases: dict[str, tuple[str, ...]],
    fund_lookup: dict[str, Fund],
    counters: dict[str, int],
    created_versions: list[int],
    auto_published_by_fund: dict[int, list[ValuationVersion]],
) -> None:
    """Persist one NAV history file as one valuation version per date.

    Dates that already have any version for the fund are skipped, so a
    re-import never disturbs published data and stays idempotent.
    """

    fund = _resolve_nav_fund(
        session, entries, path, aliases=aliases, fund_lookup=fund_lookup
    )
    if fund is None:
        counters["review"] += 1
        _audit(
            session,
            action="import.review_required",
            batch=batch,
            source_file=source_file,
            summary={
                "reason": "nav_history_product_unresolved",
                "product_candidates": [
                    entry.product_name for entry in entries if entry.product_name
                ][:3],
                "product_codes": [
                    entry.product_code for entry in entries if entry.product_code
                ][:3],
            },
            result=AuditResult.FAILURE,
        )
        return

    _recognize_source_fund(session, source_file, path, fund.id)
    existing_dates = set(
        session.scalars(
            select(ValuationVersion.valuation_date).where(
                ValuationVersion.fund_id == fund.id
            )
        )
    )
    validator = ValidationService(session)
    publisher = PublishingService(session)
    # Reported returns are rounded to two decimals of a percent, so the
    # reconciliation against 4-decimal NAVs needs a looser ratio tolerance.
    tolerances = ToleranceConfig(ratio=Decimal("0.001"))
    skipped = 0
    review = False
    published = False
    created_count = 0
    worksheet = source_file.original_filename

    for index, entry in enumerate(entries):
        if entry.valuation_date in existing_dates or (
            entry.unit_nav is None and entry.net_asset_value is None
        ):
            skipped += 1
            continue
        # 启航二号的基金每日净值表只带资产总值，没有资产净值字段；
        # 该产品负债极小（<0.3%），用总资产代替净资产以跳过人工审核。
        if (
            fund.product_code == "FS0008"
            and entry.net_asset_value is None
            and entry.total_assets is not None
        ):
            entry = replace(entry, net_asset_value=entry.total_assets)
        previous_unit_nav = entries[index - 1].unit_nav if index else None
        version = _persist_nav_entry(
            session,
            fund,
            source_file,
            entry,
            previous_unit_nav=previous_unit_nav,
            worksheet=worksheet,
        )
        created_versions.append(version.id)
        created_count += 1
        report = validator.validate_version(version.id, tolerances=tolerances)
        if report.critical_count or report.warning_count:
            review = True
        else:
            publisher.publish_version(
                version.id,
                actor_user_id=batch.created_by_user_id,
                actor_label="system:nav-import",
                reason="自动发布：净值表导入校验通过",
                schedule_analysis=False,
            )
            auto_published_by_fund.setdefault(fund.id, []).append(version)
            published = True

    counters["processed"] += 1
    if review:
        counters["review"] += 1
    if published:
        counters["published"] += 1
    _audit(
        session,
        action="import.nav_history",
        batch=batch,
        source_file=source_file,
        summary={
            "fund_id": fund.id,
            "product": fund.standard_name,
            "created_versions": created_count,
            "skipped_existing_dates": skipped,
            "review": review,
        },
    )


def _persist_nav_entry(
    session: Session,
    fund: Fund,
    source_file: SourceFile,
    entry: NavHistoryEntry,
    *,
    previous_unit_nav: Decimal | None,
    worksheet: str,
) -> ValuationVersion:
    fund_lock = select(Fund.id).where(Fund.id == fund.id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        fund_lock = fund_lock.with_for_update()
    session.scalar(fund_lock)
    version_no = (
        session.scalar(
            select(func.max(ValuationVersion.version_no)).where(
                ValuationVersion.fund_id == fund.id,
                ValuationVersion.valuation_date == entry.valuation_date,
            )
        )
        or 0
    ) + 1
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=entry.valuation_date,
        version_no=version_no,
        source_file_id=source_file.id,
        status=ValuationStatus.PARSING,
    )
    session.add(version)
    session.flush()

    # A provider that reports total assets but no liabilities still implies
    # them exactly: liabilities = assets - net assets.
    total_liabilities = entry.total_liabilities
    if (
        total_liabilities is None
        and entry.total_assets is not None
        and entry.net_asset_value is not None
    ):
        total_liabilities = entry.total_assets - entry.net_asset_value

    session.add(
        FundDailySnapshot(
            valuation_version_id=version.id,
            total_assets=entry.total_assets,
            total_liabilities=total_liabilities,
            net_asset_value=entry.net_asset_value,
            unit_nav=entry.unit_nav,
            cumulative_unit_nav=entry.cumulative_unit_nav,
            previous_unit_nav=previous_unit_nav,
            daily_return=entry.daily_return,
        )
    )
    provenance_rows: list[FieldProvenance] = []
    for field, value, transformation in (
        ("unit_nav", entry.unit_nav, "decimal"),
        ("cumulative_unit_nav", entry.cumulative_unit_nav, "decimal"),
        ("net_asset_value", entry.net_asset_value, "decimal"),
        ("total_assets", entry.total_assets, "decimal"),
        ("daily_return", entry.daily_return, "percentage_points_to_ratio"),
    ):
        if value is not None:
            provenance_rows.append(
                FieldProvenance(
                    valuation_version_id=version.id,
                    standard_field=field,
                    source_worksheet=worksheet,
                    source_row=entry.source_row,
                    raw_text=text(value),
                    transformation=transformation,
                )
            )
    if total_liabilities is not None:
        provenance_rows.append(
            FieldProvenance(
                valuation_version_id=version.id,
                standard_field="total_liabilities",
                source_worksheet=worksheet,
                source_row=entry.source_row,
                raw_text=text(total_liabilities),
                transformation=(
                    "decimal"
                    if entry.total_liabilities is not None
                    else "derived_total_assets_minus_net_asset_value"
                ),
            )
        )
    session.add_all(provenance_rows)
    for item in entry.share_classes:
        share_class = _get_or_create_share_class(session, fund, item)
        session.add(
            ShareClassDailySnapshot(
                valuation_version_id=version.id,
                share_class_id=share_class.id,
                net_assets=item.net_assets,
                unit_nav=item.unit_nav,
                cumulative_unit_nav=item.cumulative_unit_nav,
                daily_return=item.daily_return,
            )
        )
    session.flush()
    return version


def _subject_mappings(session: Session) -> tuple[SubjectMapping, ...]:
    """Load active subject rules once per batch in deterministic order."""

    return tuple(
        session.scalars(
            select(SubjectMapping)
            .where(SubjectMapping.status == "active")
            .order_by(SubjectMapping.id)
        ).all()
    )


def _active_mappings(
    mappings: tuple[SubjectMapping, ...], valuation_date: date | None
) -> tuple[SubjectMapping, ...]:
    if valuation_date is None:
        return mappings
    return tuple(
        item
        for item in mappings
        if (item.valid_from is None or item.valid_from <= valuation_date)
        and (item.valid_to is None or valuation_date <= item.valid_to)
    )


def _match_subject_mapping(
    item: ParsedSubject, mappings: tuple[SubjectMapping, ...]
) -> SubjectMapping | None:
    code = item.code.strip().casefold()
    name = item.name.strip().casefold()
    candidates = [
        mapping
        for mapping in mappings
        if (
            mapping.subject_code_or_prefix
            and code.startswith(mapping.subject_code_or_prefix.strip().casefold())
        )
        or (
            mapping.raw_name_pattern
            and mapping.raw_name_pattern.strip().casefold() in name
        )
    ]
    candidates.sort(
        key=lambda mapping: (
            -len(mapping.subject_code_or_prefix or ""),
            -len(mapping.raw_name_pattern or ""),
            mapping.id,
        )
    )
    return candidates[0] if candidates else None


def _persist_parsed_version(
    session: Session,
    fund: Fund,
    source_file: SourceFile,
    parsed: ParsedValuation,
    *,
    mappings: tuple[SubjectMapping, ...] = (),
) -> ValuationVersion:
    fund_lock = select(Fund.id).where(Fund.id == fund.id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        fund_lock = fund_lock.with_for_update()
    session.scalar(fund_lock)
    version_no = (
        session.scalar(
            select(func.max(ValuationVersion.version_no)).where(
                ValuationVersion.fund_id == fund.id,
                ValuationVersion.valuation_date == parsed.valuation_date,
            )
        )
        or 0
    ) + 1
    version = ValuationVersion(
        fund_id=fund.id,
        valuation_date=parsed.valuation_date,
        version_no=version_no,
        source_file_id=source_file.id,
        status=ValuationStatus.PARSING,
    )
    session.add(version)
    session.flush()
    session.add(
        FundDailySnapshot(
            valuation_version_id=version.id,
            total_assets=parsed.total_assets,
            total_liabilities=parsed.total_liabilities,
            net_asset_value=parsed.net_asset_value,
            unit_nav=parsed.unit_nav,
            cumulative_unit_nav=parsed.cumulative_unit_nav,
            previous_unit_nav=parsed.previous_unit_nav,
            daily_return=parsed.daily_return,
            ytd_return=parsed.ytd_return,
            mtd_return=parsed.mtd_return,
            qtd_return=parsed.qtd_return,
            wtd_return=parsed.wtd_return,
            cumulative_return=parsed.cumulative_return,
            cumulative_payout=parsed.cumulative_payout,
            available_headroom=parsed.available_headroom,
        )
    )
    session.add_all(
        FieldProvenance(
            valuation_version_id=version.id,
            standard_field=item.standard_field,
            source_worksheet=item.worksheet,
            source_row=item.row,
            source_column=item.column,
            raw_text=item.raw_text,
            transformation=item.transformation,
        )
        for item in parsed.provenance
    )
    subject_rows: list[AccountSubjectDaily] = []
    for item in parsed.subjects:
        mapping = _match_subject_mapping(item, mappings)
        subject_rows.append(
            AccountSubjectDaily(
                valuation_version_id=version.id,
                raw_subject_code=item.code,
                raw_subject_name=item.name,
                standard_category=mapping.standard_category if mapping else None,
                hierarchy_path="/".join((*item.hierarchy_path, item.code)),
                is_leaf=item.is_leaf,
                include_in_holdings=mapping.include_in_holdings if mapping else False,
                quantity=item.quantity,
                cost=item.cost,
                market_value=item.market_value,
                cost_weight=item.cost_weight,
                market_value_weight=item.market_value_weight,
                valuation_gain=item.valuation_gain,
                suspension_info=item.suspension_info,
                source_worksheet=parsed.worksheet,
                source_row=item.source_row,
            )
        )
    session.add_all(subject_rows)
    session.add_all(
        PositionDaily(
            valuation_version_id=version.id,
            security_code=item.security_code,
            security_name=item.security_name,
            market=item.market,
            account=item.account,
            original_subject_code=item.source_subject_code,
            quantity=item.quantity,
            unit_cost=item.unit_cost,
            cost=item.cost,
            market_price=item.market_price,
            market_value=item.market_value,
            nav_weight=item.nav_weight,
            valuation_gain=item.valuation_gain,
            suspension_info=item.suspension_info,
            source_worksheet=parsed.worksheet,
            source_row=item.source_row,
        )
        for item in parsed.positions
    )
    for item in parsed.share_classes:
        share_class = _get_or_create_share_class(session, fund, item)
        session.add(
            ShareClassDailySnapshot(
                valuation_version_id=version.id,
                share_class_id=share_class.id,
                net_assets=item.net_assets,
                paid_in_capital=item.paid_in_capital,
                unit_nav=item.unit_nav,
                cumulative_unit_nav=item.cumulative_unit_nav,
                previous_unit_nav=item.previous_unit_nav,
                daily_return=item.daily_return,
            )
        )
    session.flush()
    return version


def _get_or_create_share_class(
    session: Session, fund: Fund, item: ParsedShareClass
) -> ShareClass:
    share_class = session.scalar(
        select(ShareClass).where(
            ShareClass.fund_id == fund.id,
            ShareClass.share_code == item.share_code,
        )
    )
    if share_class is None:
        share_class = ShareClass(
            fund_id=fund.id,
            share_code=item.share_code,
            share_name=item.share_name,
        )
        session.add(share_class)
        session.flush()
    return share_class


def _audit(
    session: Session,
    *,
    action: str,
    batch: ImportBatch,
    source_file: SourceFile,
    summary: dict[str, object],
    result: AuditResult = AuditResult.SUCCESS,
) -> None:
    session.add(
        AuditLog(
            action=action,
            resource_type="source_file",
            resource_id=str(source_file.id),
            summary={"batch_id": batch.id, **summary},
            result=result,
        )
    )
    session.flush()


__all__ = ["BatchProcessResult", "BatchProcessingError", "process_import_batch"]


def _recognize_source_fund(
    session: Session, source_file: SourceFile, path: Path, fund_id: int
) -> None:
    # Only a complete workbook inspection is proof that the original file can
    # be shared with a restricted account. Historical valuation provenance alone
    # says nothing about other worksheets/products carried by that same file.
    try:
        source_file.recognized_fund_id = authorized_workbook_fund(
            session, path, frozenset({fund_id})
        )
    except ImportAccessError:
        source_file.recognized_fund_id = None
