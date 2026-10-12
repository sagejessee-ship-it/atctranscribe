"""Airport-area ADS-B imported per day from adsb.lol (ADR-024).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "adsb_day",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("release", sa.Text()),
        sa.Column("center_lat", sa.Double(), nullable=False),
        sa.Column("center_lon", sa.Double(), nullable=False),
        sa.Column("radius_nm", sa.Double(), nullable=False),
        sa.Column("bytes_total", sa.BigInteger()),
        sa.Column("bytes_read", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("aircraft", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("positions", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text()),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'importing', 'ready', 'failed')", name=op.f("ck_adsb_day_status")
        ),
    )
    op.create_table(
        "adsb_position",
        sa.Column(
            "day",
            sa.Date(),
            sa.ForeignKey(
                "adsb_day.day", ondelete="CASCADE", name=op.f("fk_adsb_position_day_adsb_day")
            ),
            nullable=False,
        ),
        sa.Column("t", sa.BigInteger(), nullable=False),
        sa.Column("icao24", sa.Text(), nullable=False),
        sa.Column("callsign", sa.Text()),
        sa.Column("lat", sa.REAL(), nullable=False),
        sa.Column("lon", sa.REAL(), nullable=False),
        sa.Column("alt_ft", sa.Integer()),
        sa.Column("on_ground", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("gs_kt", sa.REAL()),
        sa.Column("track_deg", sa.REAL()),
        sa.Column("vrate_fpm", sa.Integer()),
    )
    op.create_index(
        "ix_adsb_position_t",
        "adsb_position",
        ["t"],
        postgresql_using="brin",
        postgresql_with={"pages_per_range": 16},
    )
    op.create_index("ix_adsb_position_day", "adsb_position", ["day"], postgresql_using="brin")
    op.create_table(
        "adsb_aircraft",
        sa.Column(
            "day",
            sa.Date(),
            sa.ForeignKey(
                "adsb_day.day", ondelete="CASCADE", name=op.f("fk_adsb_aircraft_day_adsb_day")
            ),
            primary_key=True,
        ),
        sa.Column("icao24", sa.Text(), primary_key=True),
        sa.Column("registration", sa.Text()),
        sa.Column("type_code", sa.Text()),
        sa.Column("description", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("adsb_aircraft")
    op.drop_index("ix_adsb_position_day", table_name="adsb_position")
    op.drop_index("ix_adsb_position_t", table_name="adsb_position")
    op.drop_table("adsb_position")
    op.drop_table("adsb_day")
