"""Add the worker lease token used for import job ownership."""

import sqlalchemy as sa

from alembic import op

revision = "0003_import_job_lease"
down_revision = "0002_system_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("background_job")}
    if "lease_token" not in columns:
        op.add_column(
            "background_job",
            sa.Column("lease_token", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("background_job")}
    if "lease_token" in columns:
        op.drop_column("background_job", "lease_token")
