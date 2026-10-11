"""Persist additive runtime guardrail settings."""

from alembic import op
from talos.persistence.models import GuardrailSettingsRow

revision = "0007_guardrail_settings"
down_revision = "0006_repository_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    GuardrailSettingsRow.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    GuardrailSettingsRow.__table__.drop(bind=op.get_bind(), checkfirst=True)
