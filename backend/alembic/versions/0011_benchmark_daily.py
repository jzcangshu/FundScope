"""Add benchmark index daily data and sync state tables.

Revision ID: 0011_benchmark_daily
Revises: 0010_mail_message_link
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_benchmark_daily"
down_revision = "0010_mail_message_link"
branch_labels = None
depends_on = None

BENCHMARK_DAILY = "benchmark_daily"
SYNC_STATE = "benchmark_sync_state"


def _table_names(inspector: sa.Inspector) -> set[str]:
    return set(inspector.get_table_names())


def upgrade() -> None:
    """Create benchmark tables if they do not already exist.

    The initial migration builds the schema from the live SQLAlchemy metadata,
    so a fresh database already carries these tables.  Guard each step so the
    migration stays re-runnable on such databases while still adding the tables
    to databases created before they existed.
    """

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _table_names(inspector)

    if BENCHMARK_DAILY not in existing:
        op.create_table(
            BENCHMARK_DAILY,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ts_code", sa.String(20), nullable=False, index=True),
            sa.Column("trade_date", sa.Date(), nullable=False, index=True),
            sa.Column("close", sa.Numeric(20, 6), nullable=False),
            sa.Column("pct_chg", sa.Numeric(10, 4), nullable=True),
            sa.Column(
                "source", sa.String(50), nullable=False, server_default="tushare"
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("ts_code", "trade_date", name="uq_benchmark_code_date"),
        )

    if SYNC_STATE not in existing:
        op.create_table(
            SYNC_STATE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ts_code", sa.String(20), nullable=False),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_trade_date", sa.Date(), nullable=True),
            sa.Column("earliest_trade_date", sa.Date(), nullable=True),
            sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.String(500), nullable=True),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _table_names(inspector)

    if SYNC_STATE in existing:
        op.drop_table(SYNC_STATE)

    if BENCHMARK_DAILY in existing:
        op.drop_table(BENCHMARK_DAILY)
