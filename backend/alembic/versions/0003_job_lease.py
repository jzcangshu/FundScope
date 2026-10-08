"""Index the columns used to claim import jobs efficiently."""

import sqlalchemy as sa

from alembic import op

revision = "0003_job_lease"
down_revision = "0003_import_job_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("background_job")}
    if "ix_background_job_claim" not in indexes:
        op.create_index(
            "ix_background_job_claim",
            "background_job",
            ["status", "locked_at", "next_retry_at", "id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("background_job")}
    if "ix_background_job_claim" in indexes:
        op.drop_index("ix_background_job_claim", table_name="background_job")
