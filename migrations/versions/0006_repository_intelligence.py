"""Add versioned repository indexes and cited code chunks."""

from alembic import op

from talos.persistence.models import RepositoryChunkRow, RepositoryRow, RepositoryVersionRow

revision = "0006_repository_intelligence"
down_revision = "0005_knowledge_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    RepositoryRow.__table__.create(bind=bind, checkfirst=True)
    RepositoryVersionRow.__table__.create(bind=bind, checkfirst=True)
    RepositoryChunkRow.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    RepositoryChunkRow.__table__.drop(bind=bind, checkfirst=True)
    RepositoryVersionRow.__table__.drop(bind=bind, checkfirst=True)
    RepositoryRow.__table__.drop(bind=bind, checkfirst=True)
