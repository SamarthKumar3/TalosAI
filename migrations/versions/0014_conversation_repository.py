"""Save the selected repository across conversation reloads and runtime restarts."""

from alembic import op
from sqlalchemy import JSON, Column, inspect

revision = "0014_conversation_repository"
down_revision = "0013_message_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("conversations")}
    if "repository" not in columns:
        op.add_column("conversations", Column("repository", JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("repository")
