"""Sweeps, frozen selections, per-model runs, transcription results.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sweep_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("suite_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("selection_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effective_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_sha256", sa.Text(), nullable=False),
        sa.Column("segments_total", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "config_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_sweep_run_config_sha256_format")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'completed', 'partial', 'cancelled')",
            name=op.f("ck_sweep_run_status"),
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"], ["model_suite.id"], name=op.f("fk_sweep_run_suite_id_model_suite")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sweep_run")),
    )
    op.create_table(
        "sweep_run_model",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("execution_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_by", sa.BigInteger(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("segments_total", sa.Integer(), nullable=False),
        sa.Column("segments_completed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("segments_abstained", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("segments_error", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("audio_ms_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "inference_ms_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "runtime",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("runtime_fingerprint", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'loading', 'running', 'completed', 'failed', 'retrying')",
            name=op.f("ck_sweep_run_model_status"),
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by"], ["worker.id"], name=op.f("fk_sweep_run_model_claimed_by_worker")
        ),
        sa.ForeignKeyConstraint(
            ["model_id"], ["model.id"], name=op.f("fk_sweep_run_model_model_id_model")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["sweep_run.id"], name=op.f("fk_sweep_run_model_run_id_sweep_run")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sweep_run_model")),
        sa.UniqueConstraint(
            "run_id", "execution_order", name=op.f("uq_sweep_run_model_run_id_execution_order")
        ),
        sa.UniqueConstraint("run_id", "model_id", name=op.f("uq_sweep_run_model_run_id_model_id")),
    )
    op.create_table(
        "sweep_run_segment",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["sweep_run.id"], name=op.f("fk_sweep_run_segment_run_id_sweep_run")
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_sweep_run_segment_segment_id_segment")
        ),
        sa.PrimaryKeyConstraint("run_id", "segment_id", name=op.f("pk_sweep_run_segment")),
        sa.UniqueConstraint("run_id", "ordinal", name=op.f("uq_sweep_run_segment_run_id_ordinal")),
    )
    op.create_table(
        "transcription_result",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("sweep_run_model_id", sa.BigInteger(), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("audio_ms", sa.Integer(), nullable=True),
        sa.Column("inference_ms", sa.Integer(), nullable=True),
        sa.Column(
            "has_word_timestamps", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "has_token_confidence", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("mean_token_confidence", sa.Double(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("artifact_uri", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.Text(), nullable=True),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("audio_sha256", sa.Text(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'success' AND text IS NOT NULL AND btrim(text) <> '') OR (status = 'abstained' AND coalesce(btrim(text), '') = '') OR (status = 'error' AND error_type IS NOT NULL)",
            name=op.f("ck_transcription_result_status_semantics"),
        ),
        sa.CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_transcription_result_artifact_sha256_format"),
        ),
        sa.CheckConstraint(
            "audio_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_transcription_result_audio_sha256_format"),
        ),
        sa.CheckConstraint(
            "status IN ('success', 'abstained', 'error')",
            name=op.f("ck_transcription_result_status"),
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_transcription_result_segment_id_segment")
        ),
        sa.ForeignKeyConstraint(
            ["sweep_run_model_id"],
            ["sweep_run_model.id"],
            name=op.f("fk_transcription_result_sweep_run_model_id_sweep_run_model"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcription_result")),
        sa.UniqueConstraint(
            "sweep_run_model_id",
            "segment_id",
            name=op.f("uq_transcription_result_sweep_run_model_id_segment_id"),
        ),
    )
    op.create_index(
        "ix_transcription_result_segment", "transcription_result", ["segment_id"], unique=False
    )
    op.add_column("model", sa.Column("artifact_uri", sa.Text(), nullable=True))
    op.add_column("model", sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "model",
        sa.Column(
            "request_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("model", "request_params")
    op.drop_column("model", "artifact_size_bytes")
    op.drop_column("model", "artifact_uri")
    op.drop_index("ix_transcription_result_segment", table_name="transcription_result")
    op.drop_table("transcription_result")
    op.drop_table("sweep_run_segment")
    op.drop_table("sweep_run_model")
    op.drop_table("sweep_run")
