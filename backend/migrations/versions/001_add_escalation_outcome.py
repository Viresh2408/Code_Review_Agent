"""Add escalation_outcome column to findings table.

Revision ID: 001
Revises:
Create Date: 2026-07-05

Adds:
  - findings.escalation_outcome  VARCHAR(10)  DEFAULT 'n/a'
      Tracks whether a Claude escalation confirmed or rejected the finding.
      Values: 'confirmed' | 'rejected' | 'n/a' (not escalated / unknown)

This is required for the Phase 6 training pipeline to produce honest
negative training examples from escalations that Claude rejected.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column(
            "escalation_outcome",
            sa.String(length=10),
            server_default="n/a",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_finding_escalation_outcome",
        "findings",
        "escalation_outcome IN ('confirmed', 'rejected', 'n/a')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_finding_escalation_outcome", "findings", type_="check")
    op.drop_column("findings", "escalation_outcome")
