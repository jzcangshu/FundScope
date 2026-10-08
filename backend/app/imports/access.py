"""Fail-closed access to immutable import files and their batch metadata."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select, true
from sqlalchemy.orm import Session

from app.auth.fund_access import visible_fund_ids
from app.db.base import UserRole, UserStatus
from app.db.models import (
    Fund,
    ImportBatch,
    ImportBatchFile,
    SourceFile,
    User,
    ValuationVersion,
)
from app.parser import ValuationParser
from app.parser.excel_reader import read_workbook
from app.parser.nav_history import (
    _cell,
    _column_map,
    _composite_header,
    _match_table,
    _parse_key_value_worksheet,
    _parse_worksheet,
)
from app.parser.normalizers import parse_date, text
from app.parser.valuation_parser import ParseError


class ImportAccessError(ValueError):
    """No product identity or current product permission could be established."""


def source_file_access_clause(session: Session, user: User):
    allowed_ids = visible_fund_ids(session, user)
    if allowed_ids is None:
        return true()
    versions = (
        select(ValuationVersion.id)
        .where(ValuationVersion.source_file_id == SourceFile.id)
        .correlate(SourceFile)
    )
    return and_(
        ~exists(versions.where(ValuationVersion.fund_id.not_in(allowed_ids))),
        SourceFile.recognized_fund_id.is_not(None),
        SourceFile.recognized_fund_id.in_(allowed_ids),
    )


def batch_access_clause(session: Session, user: User):
    """SQL predicate: all files accessible, or an empty batch owned by this user."""
    if visible_fund_ids(session, user) is None:
        return true()
    files = (
        select(ImportBatchFile.id)
        .where(ImportBatchFile.batch_id == ImportBatch.id)
        .correlate(ImportBatch)
    )
    hidden_files = files.join(
        SourceFile, SourceFile.id == ImportBatchFile.source_file_id
    ).where(~source_file_access_clause(session, user))
    return and_(
        ~exists(hidden_files),
        or_(exists(files), ImportBatch.created_by_user_id == user.id),
    )


def require_batch_access(
    session: Session, user: User, batch_id: int, *, write: bool = False
) -> ImportBatch:
    batch = session.scalar(
        select(ImportBatch).where(
            ImportBatch.id == batch_id, batch_access_clause(session, user)
        )
    )
    if batch is None or (
        write
        and visible_fund_ids(session, user) is not None
        and batch.created_by_user_id != user.id
    ):
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


def actor_fund_ids(
    session: Session, actor_user_id: int | None
) -> frozenset[int] | None:
    """Recheck background work against the creator's current account and grants."""
    if actor_user_id is None:  # Trusted system/mail intake has no interactive actor.
        return None
    actor = session.get(User, actor_user_id, populate_existing=True)
    if (
        actor is None
        or actor.status != UserStatus.ACTIVE
        or actor.role
        not in {
            UserRole.ADMIN,
            UserRole.OPERATOR,
        }
    ):
        return frozenset()
    return visible_fund_ids(session, actor)


def authorized_workbook_fund(
    session: Session, path: Path, allowed_ids: frozenset[int]
) -> int:
    """Resolve workbook contents before linking or processing restricted uploads.

    Never trust the upload filename. Every NAV entry must resolve consistently;
    inspecting all worksheet cells also rejects another known product hidden in
    extra sheets or in NAV rows that the legacy single-product parser combines.
    """
    from app.imports.processor import _load_product_identity, _resolve_fund

    if not allowed_ids:
        raise ImportAccessError("product_access_denied")
    aliases, lookup = _load_product_identity(session)
    try:
        worksheets = read_workbook(path)
        codes = {
            fund.product_code.strip().casefold(): fund
            for fund in session.scalars(select(Fund))
            if fund.product_code
        }
        matched = set()
        resolved_ids = set()
        parser = ValuationParser(aliases)
        for sheet in worksheets:
            if not any(text(value) for row in sheet.rows for value in row):
                continue
            for row in sheet.rows:
                for value in row:
                    if not isinstance(value, str):
                        continue
                    if code_fund := codes.get(value.strip().casefold()):
                        matched.add(code_fund.id)
                    for name, names in aliases.items():
                        if any(alias and alias in value for alias in (name, *names)):
                            matched.add(lookup[name.strip().casefold()].id)
            try:
                parser._find_header(sheet)
            except ParseError:
                entries = _parse_worksheet(sheet) or _parse_key_value_worksheet(sheet)
            else:
                _, product_name, _ = parser._read_identity(sheet, path)
                fund = _resolve_fund(session, product_name, lookup=lookup)
                if fund is None or fund.id not in allowed_ids:
                    raise ImportAccessError("product_access_denied")
                resolved_ids.add(fund.id)
                continue
            if not entries:
                # Unknown extra worksheets cannot silently accompany an
                # authorized first worksheet into the raw-file download path.
                raise ImportAccessError("product_access_denied")
            for entry in entries:
                by_name = _resolve_fund(session, entry.product_name, lookup=lookup)
                by_code = codes.get((entry.product_code or "").strip().casefold())
                if by_name and by_code and by_name.id != by_code.id:
                    raise ImportAccessError("product_access_denied")
                fund = by_name or by_code
                if fund is None or fund.id not in allowed_ids:
                    raise ImportAccessError("product_access_denied")
                resolved_ids.add(fund.id)
                for share in entry.share_classes:
                    if not any(
                        share.share_name.startswith(name)
                        for name in (fund.standard_name, *aliases[fund.standard_name])
                    ):
                        raise ImportAccessError("product_access_denied")
            # The legacy NAV parser groups a date's rows before choosing its
            # product name. Check each original identity, including rows it
            # would otherwise discard when two unrelated funds share a date.
            for index, row in enumerate(sheet.rows):
                header, start = row, index + 1
                table = _match_table(header)
                if table is None and index + 1 < len(sheet.rows):
                    header = _composite_header(row, sheet.rows[index + 1])
                    table = _match_table(header)
                    start += 1
                if table is None:
                    continue
                columns = _column_map(header, table)
                for values in sheet.rows[start:]:
                    if (
                        "date" in columns
                        and parse_date(_cell(values, columns["date"])) is None
                    ):
                        continue
                    name = text(_cell(values, columns.get("product_name")))
                    code = text(_cell(values, columns.get("product_code"))).casefold()
                    if not name and not code:
                        continue
                    candidate = lookup.get(name.strip().casefold()) or codes.get(code)
                    if candidate is not None:
                        resolved_ids.add(candidate.id)
                    elif not name or not any(
                        name.startswith(alias)
                        for entry in entries
                        for alias in (entry.product_name,)
                        if alias
                    ):
                        raise ImportAccessError("product_access_denied")
                break
        if (
            len(resolved_ids) != 1
            or resolved_ids - allowed_ids
            or matched - resolved_ids
        ):
            raise ImportAccessError("product_access_denied")
        return resolved_ids.pop()
    except ImportAccessError:
        raise
    except Exception as exc:
        raise ImportAccessError("product_access_denied") from exc
