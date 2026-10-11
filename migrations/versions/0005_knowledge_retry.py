"""Add durable retry time for transient knowledge ingestion failures."""

from alembic import op
from sqlalchemy import Column, DateTime, inspect

revision = "0005_knowledge_retry"
down_revision = "0004_tool_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "retry_at" not in {column["name"] for column in inspect(op.get_bind()).get_columns("jobs")}:
        op.add_column("jobs", Column("retry_at", DateTime(timezone=True)))


def downgrade() -> None:
    if "retry_at" in {column["name"] for column in inspect(op.get_bind()).get_columns("jobs")}:
        op.drop_column("jobs", "retry_at")
