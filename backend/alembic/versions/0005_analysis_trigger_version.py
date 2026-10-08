"""Link analysis runs to the valuation publication that triggered them."""

import sqlalchemy as sa

from alembic import op

revision = "0005_analysis_trigger_version"
down_revision = "0004_risk_system_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("analysis_run")}
    if "trigger_version_id" not in columns:
        op.add_column(
            "analysis_run",
            sa.Column("trigger_version_id", sa.Integer(), nullable=True),
        )

    foreign_keys = sa.inspect(bind).get_foreign_keys("analysis_run")
    has_trigger_foreign_key = any(
        foreign_key["constrained_columns"] == ["trigger_version_id"]
        for foreign_key in foreign_keys
    )
    if not has_trigger_foreign_key:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("analysis_run") as batch:
                batch.create_foreign_key(
                    "fk_analysis_run_trigger_version_id",
                    "valuation_version",
                    ["trigger_version_id"],
                    ["id"],
                    ondelete="RESTRICT",
                )
        else:
            op.create_foreign_key(
                "fk_analysis_run_trigger_version_id",
                "analysis_run",
                "valuation_version",
                ["trigger_version_id"],
                ["id"],
                ondelete="RESTRICT",
            )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("analysis_run")}
    if "ix_analysis_run_trigger_version_id" not in indexes:
        op.create_index(
            "ix_analysis_run_trigger_version_id",
            "analysis_run",
            ["trigger_version_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("analysis_run")}
    if "trigger_version_id" not in columns:
        return

    indexes = {index["name"] for index in inspector.get_indexes("analysis_run")}
    if "ix_analysis_run_trigger_version_id" in indexes:
        op.drop_index("ix_analysis_run_trigger_version_id", table_name="analysis_run")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("analysis_run") as batch:
            batch.drop_constraint(
                "fk_analysis_run_trigger_version_id",
                type_="foreignkey",
            )
            batch.drop_column("trigger_version_id")
    else:
        op.drop_constraint(
            "fk_analysis_run_trigger_version_id",
            "analysis_run",
            type_="foreignkey",
        )
        op.drop_column("analysis_run", "trigger_version_id")
