"""Create Phase 3 durable runtime tables."""

from alembic import op

from talos.persistence.models import Base

revision = "0001_phase3_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
