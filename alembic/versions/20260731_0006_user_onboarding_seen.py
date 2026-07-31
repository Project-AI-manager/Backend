"""Store the first product-tour display per user.

Revision ID: 20260731_0006
Revises: 20260730_0005
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260731_0006"
down_revision: str | None = "20260730_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("onboarding_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text('UPDATE "user" SET onboarding_seen_at = CURRENT_TIMESTAMP'))


def downgrade() -> None:
    op.drop_column("user", "onboarding_seen_at")
