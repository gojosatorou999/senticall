"""initial reports and outbox tables

Revision ID: 2026081501
Revises:
Create Date: 2026-08-15

Mirrors `Base.metadata.create_all` at this point in time. The
`test_migration_matches_metadata` test compares this migration's
output against `create_all` to catch drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026081501"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("report_id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("short_ref", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("call_sid", sa.String(length=64), nullable=False),
        sa.Column("caller_hash", sa.String(length=128), nullable=False),
        sa.Column("hazard_type", sa.String(length=32), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=True),
        sa.Column("water_depth_cm", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("location_raw", sa.String(length=500), nullable=True),
        sa.Column("location_resolved", sa.String(length=500), nullable=True),
        sa.Column("dedupe_group_id", sa.String(length=64), nullable=True),
        sa.Column("priority_score", sa.Integer(), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("short_ref", name="uq_reports_short_ref"),
    )
    op.create_index("ix_reports_call_sid", "reports", ["call_sid"])
    op.create_index("ix_reports_caller_hash", "reports", ["caller_hash"])
    op.create_index("ix_reports_created_at", "reports", ["created_at"])

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["reports.report_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_outbox_report_id", "outbox", ["report_id"])
    op.create_index("ix_outbox_pending", "outbox", ["dispatched_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_index("ix_outbox_report_id", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_reports_created_at", table_name="reports")
    op.drop_index("ix_reports_caller_hash", table_name="reports")
    op.drop_index("ix_reports_call_sid", table_name="reports")
    op.drop_table("reports")
