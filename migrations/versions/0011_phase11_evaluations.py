"""Add durable Phase 11 evaluation datasets, runs, and results."""

from alembic import op

from talos.persistence.models import (
    EvaluationCaseRow,
    EvaluationDatasetRow,
    EvaluationResultRow,
    EvaluationRunRow,
)

revision = "0011_phase11_evaluations"
down_revision = "0010_runtime_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    EvaluationDatasetRow.__table__.create(bind=bind, checkfirst=True)
    EvaluationCaseRow.__table__.create(bind=bind, checkfirst=True)
    EvaluationRunRow.__table__.create(bind=bind, checkfirst=True)
    EvaluationResultRow.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    EvaluationResultRow.__table__.drop(bind=bind, checkfirst=True)
    EvaluationRunRow.__table__.drop(bind=bind, checkfirst=True)
    EvaluationCaseRow.__table__.drop(bind=bind, checkfirst=True)
    EvaluationDatasetRow.__table__.drop(bind=bind, checkfirst=True)
