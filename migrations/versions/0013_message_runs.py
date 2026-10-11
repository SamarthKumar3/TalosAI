"""Associate responses with runs and index historical lookup.

Legacy responses retain NULL: their exact run association was not recorded.
Inspect existing columns because the baseline creates current model metadata.
"""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0013_message_runs"
down_revision = "0012_message_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "run_id" not in columns:
        with op.batch_alter_table("messages") as batch:
            batch.add_column(Column("run_id", String(36), nullable=True))
            batch.create_foreign_key(
                "fk_messages_run", "agent_runs", ["run_id"], ["id"], ondelete="SET NULL"
            )
    indexes = {index["name"] for index in inspect(bind).get_indexes("messages")}
    if "ix_messages_run_id" not in indexes:
        op.create_index("ix_messages_run_id", "messages", ["run_id"])
    if "ix_messages_response_history" not in indexes:
        op.create_index(
            "ix_messages_response_history", "messages", ["conversation_id", "role", "sequence"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in inspect(bind).get_indexes("messages")}
    for name in ("ix_messages_response_history", "ix_messages_run_id"):
        if name in indexes:
            op.drop_index(name, table_name="messages")
    columns = {column["name"] for column in inspect(bind).get_columns("messages")}
    if "run_id" in columns:
        with op.batch_alter_table("messages") as batch:
            batch.drop_constraint("fk_messages_run", type_="foreignkey")
            batch.drop_column("run_id")
