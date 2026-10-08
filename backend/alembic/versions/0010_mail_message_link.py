"""Link import batches created by mail sync back to their source message.

Revision ID: 0010_mail_message_link
Revises: 0009_cleanup_settings_schedule
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_mail_message_link"
down_revision = "0009_cleanup_settings_schedule"
branch_labels = None
depends_on = None

TABLE_NAME = "import_batch"
REFERENCED_TABLE = "source_message"
COLUMN_NAME = "source_message_id"
FOREIGN_KEY_NAME = "fk_import_batch_source_message_id"
INDEX_NAME = "ix_import_batch_source_message_id"


def _column_names(inspector: sa.Inspector) -> set[str]:
    return {column["name"] for column in inspector.get_columns(TABLE_NAME)}


def _index_names(inspector: sa.Inspector) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(TABLE_NAME)}


def _foreign_key_exists(inspector: sa.Inspector) -> bool:
    return any(
        foreign_key["constrained_columns"] == [COLUMN_NAME]
        for foreign_key in inspector.get_foreign_keys(TABLE_NAME)
    )


def upgrade() -> None:
    # The initial migration builds the schema from the live SQLAlchemy metadata,
    # so a fresh database already carries this column, foreign key, and index.
    # Guard each step so the migration stays re-runnable on such databases while
    # still adding the association to databases created before it existed.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if COLUMN_NAME not in _column_names(inspector):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Integer(), nullable=True))

    if not _foreign_key_exists(inspector):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(TABLE_NAME) as batch:
                batch.create_foreign_key(
                    FOREIGN_KEY_NAME,
                    REFERENCED_TABLE,
                    [COLUMN_NAME],
                    ["id"],
                    ondelete="SET NULL",
                )
        else:
            op.create_foreign_key(
                FOREIGN_KEY_NAME,
                TABLE_NAME,
                REFERENCED_TABLE,
                [COLUMN_NAME],
                ["id"],
                ondelete="SET NULL",
            )

    if INDEX_NAME not in _index_names(inspector):
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if COLUMN_NAME not in _column_names(inspector):
        return

    if INDEX_NAME in _index_names(inspector):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)

    if bind.dialect.name == "sqlite":
        # SQLite cannot drop a named constraint; rebuilding the table without
        # the column removes its foreign key as well.
        with op.batch_alter_table(TABLE_NAME) as batch:
            batch.drop_column(COLUMN_NAME)
        return

    if _foreign_key_exists(inspector):
        op.drop_constraint(FOREIGN_KEY_NAME, TABLE_NAME, type_="foreignkey")
    op.drop_column(TABLE_NAME, COLUMN_NAME)
