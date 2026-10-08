"""Add account product scopes while preserving existing account access."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_user_fund_access"
down_revision = "0011_benchmark_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("user_account")}
    if "fund_access_mode" not in columns:
        # An inline check works with SQLite ALTER ADD COLUMN without rebuilding
        # the account table referenced by existing sessions and audit records.
        op.add_column(
            "user_account",
            sa.Column(
                "fund_access_mode",
                sa.String(8),
                sa.CheckConstraint(
                    "fund_access_mode IN ('all', 'selected')", name="ck_fundaccessmode"
                ),
                nullable=False,
                server_default="all",
            ),
        )
    if "user_fund_access" not in inspector.get_table_names():
        op.create_table(
            "user_fund_access",
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("user_account.id", ondelete="RESTRICT"),
                primary_key=True,
            ),
            sa.Column(
                "fund_id",
                sa.Integer(),
                sa.ForeignKey("fund.id", ondelete="RESTRICT"),
                primary_key=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_user_fund_access_fund_id", "user_fund_access", ["fund_id"])
    source_columns = {column["name"] for column in inspector.get_columns("source_file")}
    if "recognized_fund_id" not in source_columns:
        with op.batch_alter_table("source_file") as batch:
            batch.add_column(
                sa.Column("recognized_fund_id", sa.Integer(), nullable=True)
            )
            batch.create_foreign_key(
                "fk_source_file_recognized_fund_id",
                "fund",
                ["recognized_fund_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_index(
                "ix_source_file_recognized_fund_id", ["recognized_fund_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("user_account")}
    if "fund_access_mode" in columns:
        restricted = bind.execute(
            sa.text(
                "SELECT 1 FROM user_account WHERE fund_access_mode = 'selected' LIMIT 1"
            )
        ).first()
        if restricted:
            raise RuntimeError(
                "Cannot remove product permissions while scoped accounts exist"
            )
    if "user_fund_access" in inspector.get_table_names():
        op.drop_table("user_fund_access")
    source_columns = {column["name"] for column in inspector.get_columns("source_file")}
    if "recognized_fund_id" in source_columns:
        with op.batch_alter_table(
            "source_file",
            naming_convention={"fk": "fk_%(table_name)s_%(column_0_name)s"},
        ) as batch:
            batch.drop_index("ix_source_file_recognized_fund_id")
            batch.drop_constraint(
                "fk_source_file_recognized_fund_id", type_="foreignkey"
            )
            batch.drop_column("recognized_fund_id")
    if "fund_access_mode" in columns:
        with op.batch_alter_table("user_account") as batch:
            batch.drop_constraint("ck_fundaccessmode", type_="check")
            batch.drop_column("fund_access_mode")
