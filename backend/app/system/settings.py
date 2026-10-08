"""Validated, non-sensitive settings stored in the singleton system row."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import SystemState


class SystemSettingsError(ValueError):
    """Raised when a setting key or value is outside the supported contract."""


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    kind: str
    minimum: int | None = None
    maximum: int | None = None


SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    "source_retention_days": SettingDefinition("int", 1, 3650),
    "mail_sync_schedule": SettingDefinition("json"),
    "mail_sync_enabled": SettingDefinition("bool"),
    "backup_retention_days": SettingDefinition("int", 1, 3650),
    "timezone": SettingDefinition("timezone"),
}

DEFAULT_MAIL_SYNC_SCHEDULE: dict[str, object] = {
    "mode": "interval",
    "interval_minutes": 30,
}

DEFAULT_VALUES: dict[str, object] = {
    "mail_sync_schedule": dict(DEFAULT_MAIL_SYNC_SCHEDULE),
    "mail_sync_enabled": True,
    "backup_retention_days": 30,
    "timezone": "Asia/Shanghai",
}

MAIL_USERNAME_SETTING = "mail_imap_username"

RUNTIME_NOTE = (
    "Mail scheduling settings are read before each scheduled run. Source and backup "
    "retention settings are read when each maintenance operation starts."
)


def _baseline_values(runtime_settings: Settings) -> dict[str, object]:
    return {
        **DEFAULT_VALUES,
        "source_retention_days": runtime_settings.source_retention_days,
    }


def _source_for_baseline(key: str) -> str:
    if key == "source_retention_days":
        return "environment" if "SOURCE_RETENTION_DAYS" in os.environ else "default"
    return "default"


def _validate_mail_sync_schedule(value: object) -> dict[str, object]:
    """Validate and normalize a mail_sync_schedule value."""
    if not isinstance(value, dict):
        raise SystemSettingsError("invalid_type:mail_sync_schedule")
    mode = value.get("mode")
    if mode not in ("interval", "scheduled"):
        raise SystemSettingsError("invalid_type:mail_sync_schedule")
    if mode == "interval":
        interval = value.get("interval_minutes")
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise SystemSettingsError("invalid_type:mail_sync_schedule")
        if not 1 <= interval <= 1440:
            raise SystemSettingsError("out_of_range:mail_sync_schedule")
        return {"mode": "interval", "interval_minutes": interval}
    # scheduled mode
    raw_times = value.get("times")
    if not isinstance(raw_times, list) or not raw_times:
        raise SystemSettingsError("invalid_type:mail_sync_schedule")
    validated_times: list[dict[str, object]] = []
    for entry in raw_times:
        if not isinstance(entry, dict):
            raise SystemSettingsError("invalid_type:mail_sync_schedule")
        time_str = entry.get("time")
        if not isinstance(time_str, str) or len(time_str) != 5 or time_str[2] != ":":
            raise SystemSettingsError("invalid_type:mail_sync_schedule")
        try:
            hour = int(time_str[:2])
            minute = int(time_str[3:])
        except ValueError:
            raise SystemSettingsError("invalid_type:mail_sync_schedule")
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise SystemSettingsError("invalid_type:mail_sync_schedule")
        raw_days = entry.get("days", [])
        if not isinstance(raw_days, list):
            raise SystemSettingsError("invalid_type:mail_sync_schedule")
        days: list[int] = []
        for d in raw_days:
            if isinstance(d, bool) or not isinstance(d, int):
                raise SystemSettingsError("invalid_type:mail_sync_schedule")
            if not 0 <= d <= 7:
                raise SystemSettingsError("out_of_range:mail_sync_schedule")
            days.append(d)
        validated_times.append({"time": f"{hour:02d}:{minute:02d}", "days": days})
    return {"mode": "scheduled", "times": validated_times}


def validate_updates(values: dict[str, object]) -> dict[str, object]:
    """Validate and normalize a partial setting update."""

    normalized: dict[str, object] = {}
    for key, value in values.items():
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            raise SystemSettingsError(f"unknown_setting:{key}")
        if definition.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise SystemSettingsError(f"invalid_type:{key}")
            if definition.minimum is None or definition.maximum is None:
                raise SystemSettingsError(f"misconfigured_setting:{key}")
            if not definition.minimum <= value <= definition.maximum:
                raise SystemSettingsError(f"out_of_range:{key}")
            normalized[key] = value
            continue
        if definition.kind == "bool":
            if not isinstance(value, bool):
                raise SystemSettingsError(f"invalid_type:{key}")
            normalized[key] = value
            continue
        if definition.kind == "json":
            if key == "mail_sync_schedule":
                normalized[key] = _validate_mail_sync_schedule(value)
            else:
                normalized[key] = value
            continue
        if not isinstance(value, str) or not value.strip():
            raise SystemSettingsError(f"invalid_type:{key}")
        timezone = value.strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise SystemSettingsError(f"invalid_timezone:{key}") from exc
        normalized[key] = timezone
    return normalized


def _state(session: Session) -> SystemState:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    return state


def effective_settings(
    session: Session, runtime_settings: Settings
) -> dict[str, dict[str, object]]:
    """Return whitelisted values with their persisted/environment source."""

    state = session.get(SystemState, 1)
    persisted: dict[str, Any] = (
        state.settings if state is not None and isinstance(state.settings, dict) else {}
    )
    baseline = _baseline_values(runtime_settings)
    result: dict[str, dict[str, object]] = {}
    for key in SETTING_DEFINITIONS:
        value = baseline[key]
        source = _source_for_baseline(key)
        if key in persisted:
            try:
                value = validate_updates({key: persisted[key]})[key]
                source = "database"
            except SystemSettingsError:
                # Invalid legacy data must not make the settings endpoint fail open.
                value = baseline[key]
        result[key] = {"value": value, "source": source}
    return result


def update_settings(
    session: Session,
    runtime_settings: Settings,
    values: dict[str, object],
) -> dict[str, dict[str, object]]:
    normalized = validate_updates(values)
    state = _state(session)
    current = state.settings if isinstance(state.settings, dict) else {}
    state.settings = {**current, **normalized}
    session.flush()
    return effective_settings(session, runtime_settings)


def mail_sync_enabled(session: Session) -> bool:
    state = session.get(SystemState, 1)
    if state is None or not isinstance(state.settings, dict):
        return True
    value = state.settings.get("mail_sync_enabled", True)
    return value if isinstance(value, bool) else True


def effective_mail_sync_schedule(session: Session) -> dict[str, object]:
    """Return the validated mail sync schedule, with backward compatibility."""

    state = session.get(SystemState, 1)
    persisted = (
        state.settings if state is not None and isinstance(state.settings, dict) else {}
    )
    raw = persisted.get("mail_sync_schedule")
    if isinstance(raw, dict):
        try:
            return _validate_mail_sync_schedule(raw)
        except SystemSettingsError:
            pass
    # Backward compatibility: convert old interval_minutes to new format
    old_interval = persisted.get("mail_sync_interval_minutes")
    if isinstance(old_interval, int) and 1 <= old_interval <= 1440:
        return {"mode": "interval", "interval_minutes": old_interval}
    return dict(DEFAULT_MAIL_SYNC_SCHEDULE)


def effective_mail_username(session: Session) -> str:
    """Return the persisted IMAP username, falling back to the environment."""

    state = session.get(SystemState, 1)
    persisted = state.settings if state is not None else None
    if isinstance(persisted, dict):
        value = persisted.get(MAIL_USERNAME_SETTING)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return os.getenv("MAIL_IMAP_USERNAME", "").strip()


def update_mail_username(session: Session, username: str) -> str:
    """Persist one validated, non-sensitive IMAP username."""

    normalized = username.strip()
    if not normalized or len(normalized) > 320:
        raise SystemSettingsError("invalid_mail_username")
    state = _state(session)
    current = state.settings if isinstance(state.settings, dict) else {}
    state.settings = {**current, MAIL_USERNAME_SETTING: normalized}
    session.flush()
    return normalized


__all__ = [
    "DEFAULT_MAIL_SYNC_SCHEDULE",
    "RUNTIME_NOTE",
    "SETTING_DEFINITIONS",
    "SystemSettingsError",
    "effective_mail_sync_schedule",
    "effective_mail_username",
    "effective_settings",
    "mail_sync_enabled",
    "update_mail_username",
    "update_settings",
    "validate_updates",
]
