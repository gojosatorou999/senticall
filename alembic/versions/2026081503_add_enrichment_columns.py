"""add reports.confidence_score + reports.enriched_at

Revision ID: 2026081503
Revises: 2026081502
Create Date: 2026-08-16

P6 enrichment flow needs two new nullable columns on `reports`:

- `confidence_score` — heuristic in [0, 100] from
  `enrichment.tasks.score`.
- `enriched_at` — timestamp of the last successful enrichment run.
  NULL means the row hasn't been enriched yet.

`priority_score` already exists (added in the initial migration for
P5). `dedupe_group_id` and `location_resolved` also already exist.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026081503"
down_revision: str | None = "2026081502"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("confidence_score", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("enriched_at")
        batch.drop_column("confidence_score")
