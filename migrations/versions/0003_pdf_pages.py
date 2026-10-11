"""Record PDF page numbers on knowledge chunks."""

from alembic import op
from sqlalchemy import Column, Integer, inspect

revision = "0003_pdf_pages"
down_revision = "0002_phase6_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "page_number" not in {
        column["name"] for column in inspect(op.get_bind()).get_columns("knowledge_chunks")
    }:
        op.add_column("knowledge_chunks", Column("page_number", Integer))


def downgrade() -> None:
    if "page_number" in {
        column["name"] for column in inspect(op.get_bind()).get_columns("knowledge_chunks")
    }:
        op.drop_column("knowledge_chunks", "page_number")
