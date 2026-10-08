"""Add cooperative cancellation flag for long-running mail synchronization."""

import sqlalchemy as sa

from alembic import op

revision = "0008_mail_sync_cancel"
down_revision = "0007_validation_ignore"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("background_job")
    }
    if "cancel_requested" not in columns:
        op.add_column(
            "background_job",
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table("background_job") as batch:
                batch.alter_column("cancel_requested", server_default=None)
        else:
            op.alter_column("background_job", "cancel_requested", server_default=None)


def downgrade() -> None:
    columns = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("background_job")
    }
    if "cancel_requested" in columns:
        op.drop_column("background_job", "cancel_requested")
