"""Template-tolerant parser for the private-fund valuation workbook."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .excel_reader import WorksheetData, read_workbook
from .interface import (
    ParsedPosition,
    ParsedShareClass,
    ParsedSubject,
    ParsedValuation,
    Provenance,
)
from .normalizers import decimal, normalize_label, parse_date, ratio, text

SUMMARY_LABELS = {
    "资产类合计": "total_assets",
    "负债类合计": "total_liabilities",
    "基金资产净值": "net_asset_value",
    "基金单位净值": "unit_nav",
    "累计单位净值": "cumulative_unit_nav",
    "昨日单位净值": "previous_unit_nav",
    "净值日增长率(%)": "daily_return",
    "净值年增长率(%)": "ytd_return",
    "净值季增长率(%)": "qtd_return",
    "净值季度增长率(%)": "qtd_return",
    "净值月增长率(%)": "mtd_return",
    "净值周增长率(%)": "wtd_return",
    "累计净值增长率": "cumulative_return",
    "累计派现现金额": "cumulative_payout",
    "累计派现金额": "cumulative_payout",
    "今日可用头寸": "available_headroom",
}

SUMMARY_BASES = tuple(SUMMARY_LABELS)
SHARE_CLASS_LABELS = (
    ("基金资产净值", "net_assets"),
    ("实收资本金额", "paid_in_capital"),
    ("实收资本", "paid_in_capital"),
    ("基金单位净值", "unit_nav"),
    ("累计单位净值", "cumulative_unit_nav"),
    ("昨日单位净值", "previous_unit_nav"),
    ("净值日增长率", "daily_return"),
)
MARKET_NAMES = (
    "银行间市场",
    "上交所",
    "深交所",
    "北交所",
    "沪港通",
    "深港通",
    "港股通",
)
ACCOUNT_PATTERN = re.compile(r"(?:^|[_/\\\s-])([^_/\\\s-]*账户)(?=$|[_/\\\s-])")


class ParseError(ValueError):
    """Raised when a workbook cannot be interpreted as a valuation table."""


class ValuationParser:
    """Parse one workbook behind a small, deterministic interface."""

    def __init__(
        self, known_products: Mapping[str, Iterable[str]] | None = None
    ) -> None:
        aliases = known_products or {}
        self._aliases = {
            name: tuple(sorted({name, *values}, key=len, reverse=True))
            for name, values in aliases.items()
        }

    def parse(self, path: Path) -> ParsedValuation:
        worksheets = read_workbook(path)
        if not worksheets:
            raise ParseError("workbook_has_no_worksheets")
        worksheet = self._select_valuation_sheet(worksheets)
        header_row, columns = self._find_header(worksheet)
        valuation_date, product_name, candidates = self._read_identity(worksheet, path)
        summary_rows = self._read_summary_rows(worksheet, header_row)
        summary = self._summary_values(summary_rows, worksheet.name, columns)
        subjects = self._read_subjects(worksheet, header_row, columns, summary_rows)
        subjects_by_code = {subject.code: subject for subject in subjects}
        positions = tuple(
            self._to_position(subject, subjects_by_code)
            for subject in subjects
            if subject.is_leaf
            and subject.quantity is not None
            and subject.market_value is not None
        )
        shares = self._read_share_classes(summary_rows)
        warnings: list[str] = []
        if valuation_date is None:
            warnings.append("valuation_date_unrecognized")
        if product_name is None:
            warnings.append("product_unrecognized")
        return ParsedValuation(
            product_name=product_name,
            product_candidates=candidates,
            valuation_date=valuation_date,
            worksheet=worksheet.name,
            total_assets=summary.get("total_assets"),
            total_liabilities=summary.get("total_liabilities"),
            net_asset_value=summary.get("net_asset_value"),
            unit_nav=summary.get("unit_nav"),
            cumulative_unit_nav=summary.get("cumulative_unit_nav"),
            previous_unit_nav=summary.get("previous_unit_nav"),
            daily_return=summary.get("daily_return"),
            ytd_return=summary.get("ytd_return"),
            mtd_return=summary.get("mtd_return"),
            qtd_return=summary.get("qtd_return"),
            wtd_return=summary.get("wtd_return"),
            cumulative_return=summary.get("cumulative_return"),
            cumulative_payout=summary.get("cumulative_payout"),
            available_headroom=summary.get("available_headroom"),
            subjects=tuple(subjects),
            positions=positions,
            share_classes=shares,
            provenance=tuple(summary["_provenance"]),
            warnings=tuple(warnings),
        )

    def _select_valuation_sheet(
        self, worksheets: tuple[WorksheetData, ...]
    ) -> WorksheetData:
        for worksheet in worksheets:
            try:
                self._find_header(worksheet)
            except ParseError:
                continue
            return worksheet
        raise ParseError("valuation_header_not_found")

    @staticmethod
    def _find_header(worksheet: WorksheetData) -> tuple[int, dict[str, int]]:
        for row_index, row in enumerate(worksheet.rows):
            normalized = {
                re.sub(
                    r"(?:\((?:%|元|股|份)\)|%)$",
                    "",
                    normalize_label(value).replace("（", "(").replace("）", ")"),
                ): index
                for index, value in enumerate(row)
            }
            code_column = next(
                (index for label, index in normalized.items() if "科目代码" in label),
                None,
            )
            name_column = next(
                (index for label, index in normalized.items() if "科目名称" in label),
                None,
            )
            if code_column is None or name_column is None:
                continue
            required = {
                "quantity": ("数量", "持仓数量"),
                "unit_cost": ("单位成本",),
                "cost": ("成本", "成本金额", "总成本"),
                "cost_weight": ("成本占净值",),
                "market_price": ("市价",),
                "market_value": ("市值", "市值金额"),
                "market_weight": ("市值占净值",),
                "valuation_gain": ("估值增值",),
                "suspension_info": ("停牌信息",),
            }
            columns = {"code": code_column, "name": name_column}
            for key, labels in required.items():
                columns[key] = next(
                    (normalized[label] for label in labels if label in normalized),
                    -1,
                )
            return row_index, columns
        raise ParseError("valuation_header_not_found")

    def _read_identity(
        self, worksheet: WorksheetData, path: Path
    ) -> tuple[Any, str | None, tuple[str, ...]]:
        first_rows = [
            text(value) for row in worksheet.rows[:8] for value in row if text(value)
        ]
        combined = " ".join(first_rows)
        valuation_date = parse_date(combined)
        matches: list[str] = []
        for name, aliases in self._aliases.items():
            if any(alias and alias in combined for alias in aliases):
                matches.append(name)
        if len(matches) == 1:
            return valuation_date, matches[0], tuple(matches)
        # A filename is retained only as an unresolved candidate. It is never promoted
        # to product identity because historical file names can be misleading.
        filename_candidates = tuple(
            name
            for name, aliases in self._aliases.items()
            if any(alias and alias in path.stem for alias in aliases)
        )
        candidates = tuple(sorted(set(matches) | set(filename_candidates)))
        return valuation_date, None, candidates

    @classmethod
    def _read_summary_rows(
        cls, worksheet: WorksheetData, header_row: int
    ) -> list[tuple[int, str, tuple[object, ...]]]:
        summary_labels = {normalize_label(base) for base in SUMMARY_BASES}
        rows: list[tuple[int, str, tuple[object, ...]]] = []
        for row_index, row in enumerate(
            worksheet.rows[header_row + 1 :], header_row + 1
        ):
            label = text(row[0] if row else "")
            normalized = normalize_label(label)
            if not label or not (
                normalized in summary_labels
                or cls._match_share_class_label(normalized) is not None
            ):
                continue
            rows.append((row_index, label, row))
        return rows

    @staticmethod
    def _summary_values(
        rows: list[tuple[int, str, tuple[object, ...]]],
        worksheet_name: str,
        columns: dict[str, int],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {"_provenance": []}
        amount_fields = {
            "total_assets",
            "total_liabilities",
            "net_asset_value",
            "available_headroom",
        }
        for row_index, raw_label, row in rows:
            label = normalize_label(raw_label)
            matched = next(
                (base for base in SUMMARY_BASES if label == normalize_label(base)), None
            )
            if matched is None:
                continue
            field = SUMMARY_LABELS[matched]
            value_column = columns["market_value"] if field in amount_fields else 1
            raw_value = (
                row[value_column]
                if value_column >= 0 and len(row) > value_column
                else None
            )
            parsed = (
                ratio(raw_value) if field.endswith("return") else decimal(raw_value)
            )
            if field in {"unit_nav", "cumulative_unit_nav", "previous_unit_nav"}:
                parsed = decimal(raw_value)
            # Last occurrence wins on a duplicated summary label — newer rows
            # in a workbook generally reflect the most recently refreshed
            # value. Every occurrence is still recorded in `_provenance`.
            values[field] = parsed
            values["_provenance"].append(
                Provenance(
                    standard_field=field,
                    worksheet=worksheet_name,
                    row=row_index + 1,
                    column=value_column + 1,
                    raw_text=text(raw_value),
                    transformation="percentage_points_to_ratio"
                    if field.endswith("return")
                    else "decimal",
                )
            )
        return values

    @classmethod
    def _read_subjects(
        cls,
        worksheet: WorksheetData,
        header_row: int,
        columns: dict[str, int],
        summary_rows: list[tuple[int, str, tuple[object, ...]]],
    ) -> list[ParsedSubject]:
        summary_start = min(
            (row_index for row_index, _, _ in summary_rows), default=len(worksheet.rows)
        )
        candidates: list[tuple[int, str, str, tuple[object, ...]]] = []
        for row_index in range(header_row + 1, summary_start):
            row = worksheet.rows[row_index]
            code = (
                text(row[columns["code"]])
                if columns["code"] >= 0 and len(row) > columns["code"]
                else ""
            )
            name = (
                text(row[columns["name"]])
                if columns["name"] >= 0 and len(row) > columns["name"]
                else ""
            )
            if not code or not name or not re.fullmatch(r"[A-Za-z0-9]+", code):
                continue
            candidates.append((row_index, code, name, row))
        subjects: list[ParsedSubject] = []
        for index, (row_index, code, name, row) in enumerate(candidates):
            has_child = any(
                next_code.startswith(code) and len(next_code) > len(code)
                for _, next_code, _, _ in candidates[index + 1 :]
            )
            subjects.append(
                ParsedSubject(
                    code=code,
                    name=name,
                    quantity=decimal(cls._cell(row, columns, "quantity")),
                    unit_cost=decimal(cls._cell(row, columns, "unit_cost")),
                    cost=decimal(cls._cell(row, columns, "cost")),
                    cost_weight=ratio(cls._cell(row, columns, "cost_weight")),
                    market_value=decimal(cls._cell(row, columns, "market_value")),
                    market_price=decimal(cls._cell(row, columns, "market_price")),
                    market_value_weight=ratio(cls._cell(row, columns, "market_weight")),
                    valuation_gain=decimal(cls._cell(row, columns, "valuation_gain")),
                    suspension_info=text(cls._cell(row, columns, "suspension_info"))
                    or None,
                    is_leaf=not has_child,
                    hierarchy_path=tuple(
                        parent_code
                        for _, parent_code, _, _ in candidates[: index + 1]
                        if code.startswith(parent_code) and parent_code != code
                    ),
                    source_row=row_index + 1,
                )
            )
        return subjects

    @staticmethod
    def _cell(row: tuple[object, ...], columns: dict[str, int], key: str) -> object:
        column = columns[key]
        return row[column] if column >= 0 and len(row) > column else None

    @classmethod
    def _to_position(
        cls,
        subject: ParsedSubject,
        subjects_by_code: Mapping[str, ParsedSubject],
    ) -> ParsedPosition:
        # Only collapse to a 6-digit A-share code when the full code is exactly
        # 6 digits. Truncating longer numeric codes (e.g. broker-internal 10-digit
        # codes) caused distinct securities to collide on the same PositionDaily key.
        security_code = subject.code
        market, account = cls._position_metadata(subject, subjects_by_code)
        unit_cost = subject.unit_cost
        if unit_cost is None and subject.cost is not None and subject.quantity:
            unit_cost = subject.cost / subject.quantity
        return ParsedPosition(
            security_code=security_code,
            security_name=subject.name,
            quantity=subject.quantity,
            unit_cost=unit_cost,
            cost=subject.cost,
            market_price=subject.market_price,
            market_value=subject.market_value,
            nav_weight=subject.market_value_weight,
            valuation_gain=subject.valuation_gain,
            suspension_info=subject.suspension_info,
            source_subject_code=subject.code,
            market=market,
            account=account,
            source_row=subject.source_row,
        )

    @staticmethod
    def _position_metadata(
        subject: ParsedSubject,
        subjects_by_code: Mapping[str, ParsedSubject],
    ) -> tuple[str | None, str | None]:
        market: str | None = None
        account: str | None = None
        ancestor_names = (
            subjects_by_code[code].name
            for code in reversed(subject.hierarchy_path)
            if code in subjects_by_code
        )
        for name in ancestor_names:
            if market is None:
                market = next((item for item in MARKET_NAMES if item in name), None)
            if account is None:
                match = ACCOUNT_PATTERN.search(name)
                account = match.group(1) if match else None
            if market is not None and account is not None:
                break
        return market, account

    @staticmethod
    def _match_share_class_label(label: str) -> tuple[str, str] | None:
        normalized = normalize_label(label)
        for base, field in SHARE_CLASS_LABELS:
            prefix = normalize_label(base)
            if not normalized.startswith(prefix):
                continue
            suffix = normalized[len(prefix) :]
            if not suffix:
                return None
            if field == "daily_return":
                if suffix.startswith("(%)"):
                    suffix = suffix[3:]
                elif suffix.endswith("(%)"):
                    suffix = suffix[:-3]
                else:
                    continue
                if not suffix:
                    return None
            return field, suffix
        return None

    @classmethod
    def _read_share_classes(
        cls,
        rows: list[tuple[int, str, tuple[object, ...]]],
    ) -> tuple[ParsedShareClass, ...]:
        by_code: dict[str, dict[str, Any]] = {}
        for _, raw_label, row in rows:
            match = cls._match_share_class_label(raw_label)
            if match is None:
                continue
            field, code = match
            item = by_code.setdefault(code, {"share_code": code, "share_name": code})
            raw_value = (
                row[7]
                if field in {"net_assets", "paid_in_capital"} and len(row) > 7
                else (row[1] if len(row) > 1 else None)
            )
            parsed_value = (
                ratio(raw_value) if field == "daily_return" else decimal(raw_value)
            )
            if field not in item or item[field] is None:
                item[field] = parsed_value
        return tuple(
            ParsedShareClass(
                share_code=item["share_code"],
                share_name=item["share_name"],
                net_assets=item.get("net_assets"),
                paid_in_capital=item.get("paid_in_capital"),
                unit_nav=item.get("unit_nav"),
                cumulative_unit_nav=item.get("cumulative_unit_nav"),
                previous_unit_nav=item.get("previous_unit_nav"),
                daily_return=item.get("daily_return"),
            )
            for item in by_code.values()
        )
