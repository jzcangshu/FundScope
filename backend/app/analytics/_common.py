"""Small input helpers shared by the calculation modules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any


def field(record: Any, *names: str, default: Any = None) -> Any:
    """Read a field from a mapping or a small attribute-based record."""

    found = False
    for name in names:
        if isinstance(record, Mapping):
            if name in record:
                found = True
                if record[name] is not None:
                    return record[name]
        elif hasattr(record, name):
            found = True
            value = getattr(record, name)
            if value is not None:
                return value
    return None if found else default


def decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"decimal value must be finite: {value!r}")
        return value
    if isinstance(value, (int, float, str)):
        if isinstance(value, str) and not value.strip():
            return None
        try:
            parsed = Decimal(str(value).replace(",", "").replace("，", ""))
        except InvalidOperation as exc:
            raise ValueError(f"invalid decimal value: {value!r}") from exc
        if not parsed.is_finite():
            raise ValueError(f"decimal value must be finite: {value!r}")
        return parsed
    raise TypeError(f"expected a number or Decimal, got {type(value).__name__}")


def valuation_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid valuation date: {value!r}") from exc
    raise TypeError(f"expected date, got {type(value).__name__}")


def dated_records(records: Iterable[Any]) -> list[tuple[date, Any]]:
    """Normalize and sort records; duplicate dates are a caller error."""

    normalized = []
    for record in records:
        raw_date = field(record, "valuation_date", "date")
        if raw_date is None:
            raise ValueError("record is missing valuation_date")
        normalized.append((valuation_date(raw_date), record))

    normalized.sort(key=lambda item: item[0])
    for previous, current in pairwise(normalized):
        if previous[0] == current[0]:
            raise ValueError(f"duplicate valuation date: {current[0].isoformat()}")
    return normalized
