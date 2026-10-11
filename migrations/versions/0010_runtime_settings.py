"""Persist UI runtime feature overrides."""

from alembic import op

from talos.persistence.models import RuntimeSettingsRow

revision = "0010_runtime_settings"
down_revision = "0009_conversation_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    RuntimeSettingsRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    RuntimeSettingsRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
