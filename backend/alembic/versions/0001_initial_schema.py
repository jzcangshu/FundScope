"""Create the initial database, authentication, and import intake schema."""

from alembic import op
from app.db import models as _models  # noqa: F401
from app.db.base import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the initial schema from the canonical SQLAlchemy metadata."""

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Keep the initial schema during downgrade to avoid destructive data loss.

    The initial migration predates a table-by-table rollback history. Removing all
    tables here would silently destroy source files, published valuations, and
    audit logs, so production rollback must use an explicit forward migration.
    """
