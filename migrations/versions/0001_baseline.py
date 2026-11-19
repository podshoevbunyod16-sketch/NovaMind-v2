"""baseline schema for chats, messages and agent runs.

Revision ID: 0001_baseline
Revises: None
Create Date: 2026-11-19

Existing SQLite databases created by ``database.init_db()`` must be stamped,
not migrated: ``alembic stamp head``.  See DATABASE_MIGRATIONS.md.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=False, server_default=""),
    )
    op.create_index("idx_chats_owner", "chats", ["owner", "updated_at"])
    op.create_index("idx_chats_updated", "chats", ["updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.String(length=64), sa.ForeignKey("chats.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("idx_messages_chat", "messages", ["chat_id"])

    op.create_table(
        "chat_images",
        sa.Column("chat_id", sa.String(length=64), sa.ForeignKey("chats.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "filename"),
    )


def downgrade() -> None:
    op.drop_table("chat_images")
    op.drop_table("messages")
    op.drop_table("chats")
