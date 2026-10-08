"""Strict normalizers used by the valuation parser."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

DATE_PATTERN = re.compile(r"(?<!\d)(\d{4})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})日?")


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int | float):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    cleaned = text(value)
    if not cleaned or cleaned in {"-", "--", "N/A", "NA", "无"}:
        return None
    # When both '.' and ',' are present, the last separator is the decimal
    # mark (US "1,234.56" vs EU "1.234,56"). Anything else is ambiguous and
    # rejected rather than silently parsed 1000x off.
    has_dot = "." in cleaned
    has_comma = "," in cleaned
    has_cn_comma = "，" in cleaned
    if has_dot and has_comma:
        last_sep = max(cleaned.rfind("."), cleaned.rfind(","))
        if cleaned[last_sep] == ".":
            # US: commas are thousands separators, remove them.
            raw = cleaned.replace(",", "")
        else:
            # EU: dots are thousands separators, swap them out, then turn the
            # decimal comma into a dot.
            raw = cleaned.replace(".", "").replace(",", ".")
    elif has_comma:
        raw = cleaned.replace(",", "")
    else:
        raw = cleaned
    if has_cn_comma:
        raw = raw.replace("，", "")
    raw = raw.removesuffix("%")
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def ratio(value: Any) -> Decimal | None:
    """Convert a percentage-point cell (for example 2.5) to a ratio (0.025)."""

    parsed = decimal(value)
    return parsed / Decimal(100) if parsed is not None else None


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    match = DATE_PATTERN.search(text(value))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def normalize_label(value: Any) -> str:
    return re.sub(r"[\s:：]+", "", text(value))
