"""Add durable knowledge sources, chunks, and job leases."""

from alembic import op
from sqlalchemy import Column, DateTime, inspect
from talos.persistence.models import KnowledgeChunkRow, KnowledgeSourceRow

revision = "0002_phase6_knowledge"
down_revision = "0001_phase3_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "lease_expires_at" not in {column["name"] for column in inspector.get_columns("jobs")}:
        op.add_column("jobs", Column("lease_expires_at", DateTime(timezone=True)))
    KnowledgeSourceRow.__table__.create(bind=bind, checkfirst=True)
    KnowledgeChunkRow.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    KnowledgeChunkRow.__table__.drop(bind=bind, checkfirst=True)
    KnowledgeSourceRow.__table__.drop(bind=bind, checkfirst=True)
    if "lease_expires_at" in {column["name"] for column in inspect(bind).get_columns("jobs")}:
        op.drop_column("jobs", "lease_expires_at")
