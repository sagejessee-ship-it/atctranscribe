"""Context snapshots: cached, segment-local on-demand ADS-B answers (ADR-020).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_snapshot",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("query", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("query_sha256", sa.Text(), nullable=False),
        sa.Column("t_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("t_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("radius_nm", sa.Double(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_sha256", sa.Text(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "provider_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('adsb')", name=op.f("ck_context_snapshot_kind")),
        sa.CheckConstraint("t_end > t_start", name=op.f("ck_context_snapshot_window")),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_context_snapshot_segment_id_segment")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_snapshot")),
    )
    op.create_index(
        "ix_context_snapshot_segment",
        "context_snapshot",
        ["segment_id", "kind", "fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_context_snapshot_segment", table_name="context_snapshot")
    op.drop_table("context_snapshot")
