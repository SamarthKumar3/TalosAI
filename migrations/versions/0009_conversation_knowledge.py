"""Scope browser knowledge attachments to a conversation."""

from alembic import op
from sqlalchemy import Column, ForeignKey, String, inspect, text

revision = "0009_conversation_knowledge"
down_revision = "0008_phase9_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("knowledge_sources")}
    if "scope" not in columns:
        op.add_column(
            "knowledge_sources",
            Column("scope", String(24), nullable=False, server_default="legacy"),
        )
    if "conversation_id" not in columns:
        op.add_column(
            "knowledge_sources",
            Column(
                "conversation_id",
                String(36),
                ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_knowledge_sources_conversation_id",
            "knowledge_sources",
            ["conversation_id"],
        )
    bind.execute(text("UPDATE knowledge_sources SET scope = 'legacy' WHERE scope IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("knowledge_sources")}
    if "conversation_id" in columns:
        op.drop_index("ix_knowledge_sources_conversation_id", table_name="knowledge_sources")
        op.drop_column("knowledge_sources", "conversation_id")
    if "scope" in columns:
        op.drop_column("knowledge_sources", "scope")
