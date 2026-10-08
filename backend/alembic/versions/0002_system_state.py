"""Add the singleton state used to serialize one-time initialization."""

import sqlalchemy as sa

from alembic import op

revision = "0002_system_state"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "system_state" not in inspector.get_table_names():
        op.create_table(
            "system_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    exists = bind.execute(sa.text("SELECT 1 FROM system_state WHERE id = 1")).scalar()
    if exists is None:
        bind.execute(sa.text("INSERT INTO system_state (id) VALUES (1)"))


def downgrade() -> None:
    """Retain the singleton table; rollback must not erase initialization state."""
