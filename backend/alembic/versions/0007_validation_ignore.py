"""Record validation findings intentionally ignored during publication."""

import sqlalchemy as sa

from alembic import op

revision = "0007_validation_ignore"
down_revision = "0006_position_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("validation_result")
    }
    additions = (
        sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ignored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ignored_by_user_id", sa.Integer(), nullable=True),
        sa.Column("ignored_reason", sa.Text(), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("validation_result", column)
    foreign_keys = sa.inspect(bind).get_foreign_keys("validation_result")
    if not any(
        foreign_key["constrained_columns"] == ["ignored_by_user_id"]
        for foreign_key in foreign_keys
    ):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("validation_result") as batch:
                batch.create_foreign_key(
                    "fk_validation_result_ignored_by_user_id",
                    "user_account",
                    ["ignored_by_user_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        else:
            op.create_foreign_key(
                "fk_validation_result_ignored_by_user_id",
                "validation_result",
                "user_account",
                ["ignored_by_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("validation_result") as batch:
            batch.alter_column("ignored", server_default=None)
    else:
        op.alter_column("validation_result", "ignored", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("validation_result")
    }
    if "ignored_by_user_id" in columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("validation_result") as batch:
                batch.drop_column("ignored_by_user_id")
        else:
            op.drop_constraint(
                "fk_validation_result_ignored_by_user_id",
                "validation_result",
                type_="foreignkey",
            )
            op.drop_column("validation_result", "ignored_by_user_id")
    for name in ("ignored_reason", "ignored_at", "ignored"):
        if name in columns:
            op.drop_column("validation_result", name)
