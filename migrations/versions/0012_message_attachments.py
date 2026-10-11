"""Persist conversation-scoped document references on user messages."""

from alembic import op
from sqlalchemy import JSON, Column, inspect

revision = "0012_message_attachments"
down_revision = "0011_phase11_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "attachments" not in columns:
        op.add_column(
            "messages",
            Column("attachments", JSON, nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "attachments" in columns:
        op.drop_column("messages", "attachments")
