"""Tables and engine for the agent registry.

Two tables: immutable definition versions, and one binding per environment that
says which version is live. Promotion and rollback move the binding.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

metadata = MetaData()

agent_definitions = Table(
    "agent_definitions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("agent_key", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("config", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_by", String, nullable=False),
    Column("published_at", DateTime(timezone=True)),
    CheckConstraint("status IN ('draft', 'published', 'archived')", name="ck_agent_definitions_status"),
    UniqueConstraint("agent_key", "version", name="uq_agent_definitions_key_version"),
    Index("ix_agent_definitions_agent_key", "agent_key"),
    Index("ix_agent_definitions_config_gin", "config", postgresql_using="gin"),
)

agent_environment_bindings = Table(
    "agent_environment_bindings",
    metadata,
    Column("agent_key", String, primary_key=True),
    Column("environment", String, primary_key=True),
    Column("agent_version", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_by", String, nullable=False),
    CheckConstraint(
        "environment IN ('dev', 'test', 'staging', 'production')",
        name="ck_agent_environment_bindings_environment",
    ),
    ForeignKeyConstraint(
        ["agent_key", "agent_version"],
        ["agent_definitions.agent_key", "agent_definitions.version"],
        name="fk_agent_environment_bindings_definition",
    ),
)


def create_engine(database_url: str) -> AsyncEngine:
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set. Run `asas-agent init`, or set it in the environment.")
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
