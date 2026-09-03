"""add reports.description_clean

Revision ID: 2026081502
Revises: 2026081501
Create Date: 2026-08-15

PII-scrubbed twin of `description`. Populated at report-write time by
SqlReportSink; consumed by outbound artifacts (CSV, SSE, alerts).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026081502"
down_revision: str | None = "2026081501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("description_clean", sa.String(length=2000), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("description_clean")
