"""Distinguish provider conversations for the same customer.

Revision ID: 20260808_0009
Revises: 20260808_0008
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260808_0009"
down_revision: str | None = "20260808_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.drop_constraint("uq_conversation_tenant_channel_customer", type_="unique")
        batch_op.add_column(
            sa.Column(
                "external_conversation_id",
                sa.String(length=255),
                nullable=False,
                server_default="",
            )
        )
        batch_op.create_unique_constraint(
            "uq_conversation_tenant_channel_customer",
            ["tenant_id", "channel_id", "customer_id", "external_conversation_id"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            "SELECT tenant_id, channel_id, customer_id FROM conversation "
            "GROUP BY tenant_id, channel_id, customer_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicates is not None:
        raise RuntimeError(
            "Cannot downgrade 20260808_0009: multiple provider conversations exist "
            "for one tenant/channel/customer. Merge them explicitly before downgrade."
        )
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.drop_constraint("uq_conversation_tenant_channel_customer", type_="unique")
        batch_op.drop_column("external_conversation_id")
        batch_op.create_unique_constraint(
            "uq_conversation_tenant_channel_customer",
            ["tenant_id", "channel_id", "customer_id"],
        )
