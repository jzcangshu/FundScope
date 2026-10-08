"""Add row-level source details to subjects and positions."""

import sqlalchemy as sa

from alembic import op

revision = "0006_position_source"
down_revision = "0005_analysis_trigger_version"
branch_labels = None
depends_on = None

SUBJECT_COLUMNS = (
    sa.Column("source_worksheet", sa.String(length=255), nullable=True),
    sa.Column("source_row", sa.Integer(), nullable=True),
)
POSITION_COLUMNS = (
    sa.Column("original_subject_code", sa.String(length=100), nullable=True),
    sa.Column("source_worksheet", sa.String(length=255), nullable=True),
    sa.Column("source_row", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    subject_columns = {
        column["name"] for column in inspector.get_columns("account_subject_daily")
    }
    for column in SUBJECT_COLUMNS:
        if column.name not in subject_columns:
            op.add_column("account_subject_daily", column)

    position_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("position_daily")
    }
    for column in POSITION_COLUMNS:
        if column.name not in position_columns:
            op.add_column("position_daily", column)


def downgrade() -> None:
    bind = op.get_bind()
    subject_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("account_subject_daily")
    }
    position_columns = {
        column["name"] for column in sa.inspect(bind).get_columns("position_daily")
    }
    with op.batch_alter_table("account_subject_daily") as batch:
        for column in reversed(SUBJECT_COLUMNS):
            if column.name in subject_columns:
                batch.drop_column(column.name)
    with op.batch_alter_table("position_daily") as batch:
        for column in reversed(POSITION_COLUMNS):
            if column.name in position_columns:
                batch.drop_column(column.name)
