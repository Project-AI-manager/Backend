"""Add a unique provider identity for channel routing.

Revision ID: 20260808_0008
Revises: 20260807_0007
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0008"
down_revision: str | None = "20260807_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("channel") as batch_op:
        batch_op.add_column(sa.Column("external_identity", sa.String(length=255), nullable=True))
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                """
                UPDATE channel
                SET external_identity = 'whatsapp:' || (settings ->> 'phone_number_id')
                WHERE type = 'whatsapp'
                  AND settings ? 'phone_number_id'
                  AND NULLIF(settings ->> 'phone_number_id', '') IS NOT NULL
                """
            )
        )
    with op.batch_alter_table("channel") as batch_op:
        batch_op.create_unique_constraint(
            "uq_channel_external_identity", ["external_identity"]
        )


def downgrade() -> None:
    with op.batch_alter_table("channel") as batch_op:
        batch_op.drop_constraint("uq_channel_external_identity", type_="unique")
        batch_op.drop_column("external_identity")
