"""Add durable tool approval proposals."""

from alembic import op
from talos.persistence.models import ToolApprovalRow

revision = "0004_tool_approvals"
down_revision = "0003_pdf_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    ToolApprovalRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    ToolApprovalRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
