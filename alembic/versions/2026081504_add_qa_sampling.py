"""add reports.sampled_for_qa + qa_reviewed_at + qa_notes

Revision ID: 2026081504
Revises: 2026081503
Create Date: 2026-08-16

P6 QA sampling queue (spec §11.11 "5% QA sampling queue"). Adds three
nullable-adjacent columns to the `reports` table so SqlReportSink can
flip ~5% of writes for human review, and admin ops can mark them
reviewed with notes.

Design choices:
- `sampled_for_qa` is NOT NULL (bool). Existing rows get FALSE by
  virtue of the server_default — no back-fill migration needed.
- `qa_reviewed_at` + `qa_notes` are nullable; they populate only on
  the review action (`POST /api/v1/reports/{short_ref}/qa_review`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2026081504"
down_revision: str | None = "2026081503"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.add_column(
            sa.Column(
                "sampled_for_qa",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("qa_reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("qa_notes", sa.String(length=1000), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("qa_notes")
        batch.drop_column("qa_reviewed_at")
        batch.drop_column("sampled_for_qa")
