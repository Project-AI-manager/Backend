"""Add explainable AI decision and extended usage ledgers.

Revision ID: 20260807_0007
Revises: 20260731_0006
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260807_0007"
down_revision: str | None = "20260731_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_usage_event") as batch_op:
        batch_op.add_column(
            sa.Column("outcome", sa.String(length=24), nullable=False, server_default="completed")
        )
        batch_op.add_column(
            sa.Column("error_code", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}")
        )

    op.create_table(
        "ai_decision_event",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("tenant_id", "customer_id", "conversation_id", "message_id"):
        op.create_index(f"ix_ai_decision_event_{column}", "ai_decision_event", [column])


def downgrade() -> None:
    op.drop_table("ai_decision_event")
    with op.batch_alter_table("ai_usage_event") as batch_op:
        batch_op.drop_column("metadata_json")
        batch_op.drop_column("error_code")
        batch_op.drop_column("outcome")
