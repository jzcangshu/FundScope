"""Remove dead settings and migrate mail_sync_interval_minutes to mail_sync_schedule.

Revision ID: 0009_cleanup_settings_schedule
Revises: 0008_mail_sync_cancel
Create Date: 2026-08-27
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0009_cleanup_settings_schedule"
down_revision = "0008_mail_sync_cancel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Read current settings from the singleton system_state row.
    if dialect == "sqlite":
        row = bind.execute(
            sa.text("SELECT id, settings FROM system_state WHERE id = 1")
        ).fetchone()
    else:
        row = bind.execute(
            sa.text("SELECT id, settings FROM system_state WHERE id = 1")
        ).fetchone()

    if row is None:
        return

    raw_settings = row[1]
    if not isinstance(raw_settings, dict):
        return

    changed = False

    # Remove dead keys
    for key in ("task_concurrency", "data_lateness_days"):
        if key in raw_settings:
            del raw_settings[key]
            changed = True

    # Migrate mail_sync_interval_minutes -> mail_sync_schedule
    if "mail_sync_schedule" not in raw_settings:
        old_interval = raw_settings.get("mail_sync_interval_minutes")
        if isinstance(old_interval, int) and 1 <= old_interval <= 1440:
            raw_settings["mail_sync_schedule"] = {
                "mode": "interval",
                "interval_minutes": old_interval,
            }
        else:
            raw_settings["mail_sync_schedule"] = {
                "mode": "interval",
                "interval_minutes": 30,
            }
        changed = True

    # Remove the old key
    if "mail_sync_interval_minutes" in raw_settings:
        del raw_settings["mail_sync_interval_minutes"]
        changed = True

    if changed:
        bind.execute(
            sa.text("UPDATE system_state SET settings = :settings WHERE id = 1"),
            {"settings": json.dumps(raw_settings)},
        )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT id, settings FROM system_state WHERE id = 1")
    ).fetchone()

    if row is None:
        return

    raw_settings = row[1]
    if not isinstance(raw_settings, dict):
        return

    changed = False

    # Restore mail_sync_interval_minutes from mail_sync_schedule
    schedule = raw_settings.get("mail_sync_schedule")
    if isinstance(schedule, dict):
        interval = schedule.get("interval_minutes", 30)
        if isinstance(interval, int) and 1 <= interval <= 1440:
            raw_settings["mail_sync_interval_minutes"] = interval
        else:
            raw_settings["mail_sync_interval_minutes"] = 30
        del raw_settings["mail_sync_schedule"]
        changed = True

    # Restore dead keys with defaults
    if "task_concurrency" not in raw_settings:
        raw_settings["task_concurrency"] = 1
        changed = True
    if "data_lateness_days" not in raw_settings:
        raw_settings["data_lateness_days"] = 1
        changed = True

    if changed:
        bind.execute(
            sa.text("UPDATE system_state SET settings = :settings WHERE id = 1"),
            {"settings": json.dumps(raw_settings)},
        )
