"""Hardware-profile model qualification and ensemble eligibility (ADR-021).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_platform_qualification",
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("hardware_profile", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "hardware_profile ~ '^[a-z0-9][a-z0-9_]*$'",
            name=op.f("ck_model_platform_qualification_profile_format"),
        ),
        sa.CheckConstraint(
            "state IN ('qualified', 'qualified_cpu_only', 'qualified_with_offload', 'too_slow', 'oom', 'backend_failure', 'unsupported_on_platform', 'not_relevant', 'experimental')",
            name=op.f("ck_model_platform_qualification_state"),
        ),
        sa.ForeignKeyConstraint(
            ["model_id"], ["model.id"], name=op.f("fk_model_platform_qualification_model_id_model")
        ),
        sa.PrimaryKeyConstraint(
            "model_id", "hardware_profile", name=op.f("pk_model_platform_qualification")
        ),
    )
    op.add_column(
        "model",
        sa.Column(
            "ensemble_eligible", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("model", "ensemble_eligible")
    op.drop_table("model_platform_qualification")
