"""Persist safe system settings and risk-event handling metadata."""

import sqlalchemy as sa

from alembic import op

revision = "0004_risk_system_settings"
down_revision = "0003_job_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    system_state_columns = {
        column["name"] for column in inspector.get_columns("system_state")
    }
    if "settings" not in system_state_columns:
        op.add_column("system_state", sa.Column("settings", sa.JSON(), nullable=True))

    risk_event_columns = {
        column["name"] for column in inspector.get_columns("risk_event")
    }
    columns_to_add: list[sa.Column[object]] = []
    if "handled_by_user_id" not in risk_event_columns:
        columns_to_add.append(
            sa.Column(
                "handled_by_user_id",
                sa.Integer(),
                sa.ForeignKey(
                    "user_account.id",
                    name="fk_risk_event_handled_by_user",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    if "handled_at" not in risk_event_columns:
        columns_to_add.append(
            sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "evidence_reference" not in risk_event_columns:
        columns_to_add.append(
            sa.Column("evidence_reference", sa.String(length=1000), nullable=True)
        )
    if columns_to_add:
        # Batch mode keeps SQLite-compatible table rebuilds and also works for
        # PostgreSQL's regular ALTER TABLE path.
        with op.batch_alter_table("risk_event") as batch:
            for column in columns_to_add:
                batch.add_column(column)

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("risk_event")}
    if "ix_risk_event_handled_by_user_id" not in indexes:
        op.create_index(
            "ix_risk_event_handled_by_user_id",
            "risk_event",
            ["handled_by_user_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("risk_event")}
    if "ix_risk_event_handled_by_user_id" in indexes:
        op.drop_index("ix_risk_event_handled_by_user_id", table_name="risk_event")

    risk_event_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("risk_event")
    }
    columns_to_drop = [
        name
        for name in ("evidence_reference", "handled_at", "handled_by_user_id")
        if name in risk_event_columns
    ]
    if columns_to_drop:
        # SQLite cannot directly drop a column that participates in a foreign key;
        # batch mode rebuilds the table while preserving the remaining rows.
        with op.batch_alter_table("risk_event") as batch:
            for name in columns_to_drop:
                batch.drop_column(name)

    system_state_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("system_state")
    }
    if "settings" in system_state_columns:
        op.drop_column("system_state", "settings")
