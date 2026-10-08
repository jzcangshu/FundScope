"""Allow the boss role without changing existing account grants."""

import sqlalchemy as sa

from alembic import op

revision = "0013_boss_role"
down_revision = "0012_user_fund_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_account") as batch:
        batch.drop_constraint("ck_userrole", type_="check")
        batch.create_check_constraint(
            "ck_userrole", "role IN ('admin', 'operator', 'viewer', 'boss')"
        )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM user_account WHERE role = 'boss' LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Cannot remove boss role while boss accounts exist")
    with op.batch_alter_table("user_account") as batch:
        batch.drop_constraint("ck_userrole", type_="check")
        batch.create_check_constraint(
            "ck_userrole", "role IN ('admin', 'operator', 'viewer')"
        )
