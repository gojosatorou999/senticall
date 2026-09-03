"""add pii_erased_at to reports

Revision ID: 2026081505
Revises: 2026081504
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2026081505"
down_revision = "2026081504"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("pii_erased_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reports", "pii_erased_at")
