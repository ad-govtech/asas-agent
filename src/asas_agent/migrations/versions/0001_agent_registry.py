"""Agent registry: definitions and environment bindings.

Revision ID: 0001_agent_registry
Revises:
Create Date: 2026-09-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_agent_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_key", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_agent_definitions_status"),
        sa.UniqueConstraint("agent_key", "version", name="uq_agent_definitions_key_version"),
    )
    op.create_index("ix_agent_definitions_agent_key", "agent_definitions", ["agent_key"])
    op.create_index(
        "ix_agent_definitions_config_gin",
        "agent_definitions",
        ["config"],
        postgresql_using="gin",
    )

    op.create_table(
        "agent_environment_bindings",
        sa.Column("agent_key", sa.String(), primary_key=True),
        sa.Column("environment", sa.String(), primary_key=True),
        sa.Column("agent_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.CheckConstraint(
            "environment IN ('dev', 'test', 'staging', 'production')",
            name="ck_agent_environment_bindings_environment",
        ),
        sa.ForeignKeyConstraint(
            ["agent_key", "agent_version"],
            ["agent_definitions.agent_key", "agent_definitions.version"],
            name="fk_agent_environment_bindings_definition",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_environment_bindings")
    op.drop_index("ix_agent_definitions_config_gin", table_name="agent_definitions")
    op.drop_index("ix_agent_definitions_agent_key", table_name="agent_definitions")
    op.drop_table("agent_definitions")
