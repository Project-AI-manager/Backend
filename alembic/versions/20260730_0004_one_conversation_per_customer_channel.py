"""Keep one conversation for each tenant, channel and customer.

Revision ID: 20260730_0004
Revises: 20260727_0003
Create Date: 2026-07-30
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "20260730_0004"
down_revision: str | None = "20260727_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    _merge_duplicate_conversations(connection)
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.create_unique_constraint(
            "uq_conversation_tenant_channel_customer",
            ["tenant_id", "channel_id", "customer_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.drop_constraint(
            "uq_conversation_tenant_channel_customer",
            type_="unique",
        )


def _merge_duplicate_conversations(connection: Any) -> None:
    groups = connection.execute(
        sa.text(
            "SELECT tenant_id, channel_id, customer_id "
            "FROM conversation "
            "GROUP BY tenant_id, channel_id, customer_id "
            "HAVING COUNT(*) > 1"
        )
    ).mappings().all()
    for group in groups:
        conversations = connection.execute(
            sa.text(
                "SELECT id, status, assignee_user_id, last_message_at, "
                "last_message_preview, unread_count "
                "FROM conversation "
                "WHERE tenant_id = :tenant_id AND channel_id = :channel_id "
                "AND customer_id = :customer_id "
                "ORDER BY CASE WHEN last_message_at IS NULL THEN 1 ELSE 0 END, "
                "last_message_at DESC, created_at DESC, id DESC"
            ),
            dict(group),
        ).mappings().all()
        canonical = conversations[0]
        canonical_id = canonical["id"]
        unread_count = sum(int(item["unread_count"] or 0) for item in conversations)

        for source in conversations[1:]:
            source_id = source["id"]
            _clear_colliding_message_external_ids(connection, canonical_id, source_id)
            connection.execute(
                sa.text(
                    "UPDATE message SET conversation_id = :canonical_id "
                    "WHERE conversation_id = :source_id"
                ),
                {"canonical_id": canonical_id, "source_id": source_id},
            )
            connection.execute(
                sa.text(
                    "UPDATE kb_candidate SET conversation_id = :canonical_id "
                    "WHERE conversation_id = :source_id"
                ),
                {"canonical_id": canonical_id, "source_id": source_id},
            )
            connection.execute(
                sa.text(
                    "UPDATE escalation SET conversation_id = :canonical_id "
                    "WHERE conversation_id = :source_id"
                ),
                {"canonical_id": canonical_id, "source_id": source_id},
            )
            connection.execute(
                sa.text("DELETE FROM conversation WHERE id = :source_id"),
                {"source_id": source_id},
            )

        connection.execute(
            sa.text(
                "UPDATE conversation SET unread_count = :unread_count "
                "WHERE id = :canonical_id"
            ),
            {"canonical_id": canonical_id, "unread_count": unread_count},
        )


def _clear_colliding_message_external_ids(
    connection: Any,
    canonical_id: object,
    source_id: object,
) -> None:
    """Preserve both messages while satisfying the destination unique key."""
    connection.execute(
        sa.text(
            "UPDATE message SET external_message_id = NULL "
            "WHERE conversation_id = :source_id AND external_message_id IS NOT NULL "
            "AND external_message_id IN ("
            "SELECT external_message_id FROM message "
            "WHERE conversation_id = :canonical_id AND external_message_id IS NOT NULL"
            ")"
        ),
        {"canonical_id": canonical_id, "source_id": source_id},
    )
