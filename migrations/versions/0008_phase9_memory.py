"""Persist Phase 9 conversation summaries and explicit memory."""

from alembic import op

from talos.persistence.models import ConversationSummaryRow, MemoryRow

revision = "0008_phase9_memory"
down_revision = "0007_guardrail_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ConversationSummaryRow.__table__.create(bind=op.get_bind(), checkfirst=True)
    MemoryRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    MemoryRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
    ConversationSummaryRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
