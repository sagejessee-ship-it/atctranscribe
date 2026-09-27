"""Foundation schema: corpus index, scans, workers, model registry.

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "architecture_family",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "key ~ '^[a-z][a-z0-9_-]*$'", name=op.f("ck_architecture_family_key_format")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_architecture_family")),
    )
    op.create_table(
        "corpus_source",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("logical_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("read_only", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("adapter_type", sa.Text(), nullable=False),
        sa.Column(
            "adapter_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "adapter_type IN ('filesystem')", name=op.f("ck_corpus_source_adapter_type")
        ),
        sa.CheckConstraint(
            "logical_key ~ '^[a-z][a-z0-9_]*$'", name=op.f("ck_corpus_source_logical_key_format")
        ),
        sa.CheckConstraint("read_only", name=op.f("ck_corpus_source_read_only")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corpus_source")),
        sa.UniqueConstraint("logical_key", name=op.f("uq_corpus_source_logical_key")),
    )
    op.create_table(
        "model_suite",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9_.-]*$'", name=op.f("ck_model_suite_name_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_suite")),
        sa.UniqueConstraint("name", name=op.f("uq_model_suite_name")),
    )
    op.create_table(
        "worker",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "health",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker")),
        sa.UniqueConstraint("name", name=op.f("uq_worker_name")),
    )
    op.create_table(
        "corpus_scan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("scope_prefix", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "counters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('incremental', 'full', 'verify')", name=op.f("ck_corpus_scan_mode")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'source_unavailable', 'failed', 'abandoned')",
            name=op.f("ck_corpus_scan_status"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["corpus_source.id"], name=op.f("fk_corpus_scan_source_id_corpus_source")
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"], ["worker.id"], name=op.f("fk_corpus_scan_worker_id_worker")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_corpus_scan")),
    )
    op.create_index(
        "uq_corpus_scan_one_running",
        "corpus_scan",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "model",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("logical_name", sa.Text(), nullable=False),
        sa.Column("architecture_family", sa.Text(), nullable=False),
        sa.Column("crisp_backend", sa.Text(), nullable=False),
        sa.Column("model_filename", sa.Text(), nullable=False),
        sa.Column("model_sha256", sa.Text(), nullable=True),
        sa.Column("upstream_model", sa.Text(), nullable=True),
        sa.Column("upstream_revision", sa.Text(), nullable=True),
        sa.Column("quantization", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "pedigree",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sweep_eligible", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("experimental", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "model_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_model_model_sha256_format")
        ),
        sa.CheckConstraint(
            "NOT sweep_eligible OR model_sha256 IS NOT NULL",
            name=op.f("ck_model_sweep_requires_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["architecture_family"],
            ["architecture_family.key"],
            name=op.f("fk_model_architecture_family_architecture_family"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model")),
        sa.UniqueConstraint("logical_name", name=op.f("uq_model_logical_name")),
    )
    op.create_table(
        "corpus_directory",
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("relative_dir", sa.Text(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column(
            "subdirs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_scan_id", sa.BigInteger(), nullable=False),
        sa.Column("last_listed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["last_scan_id"],
            ["corpus_scan.id"],
            name=op.f("fk_corpus_directory_last_scan_id_corpus_scan"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["corpus_source.id"],
            name=op.f("fk_corpus_directory_source_id_corpus_source"),
        ),
        sa.PrimaryKeyConstraint("source_id", "relative_dir", name=op.f("pk_corpus_directory")),
    )
    op.create_table(
        "model_suite_member",
        sa.Column("suite_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("execution_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "execution_order >= 0", name=op.f("ck_model_suite_member_execution_order")
        ),
        sa.ForeignKeyConstraint(
            ["model_id"], ["model.id"], name=op.f("fk_model_suite_member_model_id_model")
        ),
        sa.ForeignKeyConstraint(
            ["suite_id"],
            ["model_suite.id"],
            name=op.f("fk_model_suite_member_suite_id_model_suite"),
        ),
        sa.PrimaryKeyConstraint("suite_id", "model_id", name=op.f("pk_model_suite_member")),
        sa.UniqueConstraint(
            "suite_id",
            "execution_order",
            name=op.f("uq_model_suite_member_suite_id_execution_order"),
        ),
    )
    op.create_table(
        "segment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("relative_dir", sa.Text(), nullable=False),
        sa.Column("source_recording_id", sa.Text(), nullable=True),
        sa.Column("capture_start_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capture_end_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("temporal_status", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("frequency_hz", sa.BigInteger(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("station", sa.Text(), nullable=True),
        sa.Column(
            "presence_status", sa.Text(), server_default=sa.text("'present'"), nullable=False
        ),
        sa.Column("integrity_status", sa.Text(), server_default=sa.text("'ok'"), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_scan_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "(capture_start_utc IS NOT NULL) = (temporal_status IN ('resolved', 'unverified'))",
            name=op.f("ck_segment_utc_matches_temporal_status"),
        ),
        sa.CheckConstraint(
            "(relative_dir = '' AND strpos(relative_path, '/') = 0) OR (left(relative_path, length(relative_dir) + 1) = relative_dir || '/' AND strpos(substr(relative_path, length(relative_dir) + 2), '/') = 0)",
            name=op.f("ck_segment_relative_dir_is_parent"),
        ),
        sa.CheckConstraint(
            "integrity_status IN ('ok', 'changed')", name=op.f("ck_segment_integrity_status")
        ),
        sa.CheckConstraint(
            "presence_status IN ('present', 'missing')", name=op.f("ck_segment_presence_status")
        ),
        sa.CheckConstraint(
            "relative_path <> '' AND left(relative_path, 1) <> '/' AND strpos(relative_path, '\\') = 0 AND relative_path !~ '(^|/)\\.{0,2}(/|$)'",
            name=op.f("ck_segment_relative_path_format"),
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_segment_sha256_format")),
        sa.CheckConstraint(
            "temporal_status IN ('resolved', 'unverified', 'ambiguous', 'unresolved')",
            name=op.f("ck_segment_temporal_status"),
        ),
        sa.CheckConstraint(
            "capture_end_utc IS NULL OR (capture_start_utc IS NOT NULL AND capture_end_utc >= capture_start_utc)",
            name=op.f("ck_segment_capture_interval"),
        ),
        sa.CheckConstraint("duration_ms >= 0", name=op.f("ck_segment_duration_ms")),
        sa.CheckConstraint("file_size >= 0", name=op.f("ck_segment_file_size")),
        sa.ForeignKeyConstraint(
            ["last_seen_scan_id"],
            ["corpus_scan.id"],
            name=op.f("fk_segment_last_seen_scan_id_corpus_scan"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["corpus_source.id"], name=op.f("fk_segment_source_id_corpus_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segment")),
        sa.UniqueConstraint(
            "source_id", "relative_path", name=op.f("uq_segment_source_id_relative_path")
        ),
    )
    op.create_index("ix_segment_capture_start_utc", "segment", ["capture_start_utc"], unique=False)
    op.create_index("ix_segment_source_dir", "segment", ["source_id", "relative_dir"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_segment_source_dir", table_name="segment")
    op.drop_index("ix_segment_capture_start_utc", table_name="segment")
    op.drop_table("segment")
    op.drop_table("model_suite_member")
    op.drop_table("corpus_directory")
    op.drop_table("model")
    op.drop_index(
        "uq_corpus_scan_one_running",
        table_name="corpus_scan",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_table("corpus_scan")
    op.drop_table("worker")
    op.drop_table("model_suite")
    op.drop_table("corpus_source")
    op.drop_table("architecture_family")
