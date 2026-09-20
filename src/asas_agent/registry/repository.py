"""Reading and writing agent definitions.

Rules this file enforces:
- a published version is never edited, a new version is created instead;
- publishing validates the configuration again and pins a moving prompt label
  to the version that resolved at that moment;
- an environment points at exactly one published version.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from asas_agent.integrations.models import ModelRegistry
from asas_agent.integrations.prompts import PromptProvider, pin_prompt

from .capabilities import registry as capability_registry
from .db import agent_definitions, agent_environment_bindings
from .outputs import registry as output_registry
from .schema import AgentConfig, Environment
from .validation import DefinitionValidator


class RegistryError(RuntimeError):
    """Raised when a registry operation is not allowed."""


@dataclass(frozen=True)
class AgentDefinition:
    agent_key: str
    version: int
    status: str
    config: AgentConfig
    created_at: datetime
    created_by: str
    published_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> AgentDefinition:
        return cls(
            agent_key=row.agent_key,
            version=row.version,
            status=row.status,
            config=AgentConfig.model_validate(row.config),
            created_at=row.created_at,
            created_by=row.created_by,
            published_at=row.published_at,
        )


class AgentRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        *,
        prompts: PromptProvider | None = None,
        models: ModelRegistry | None = None,
        validator: DefinitionValidator | None = None,
    ):
        self._session_factory = session_factory
        self._prompts = prompts
        self._validator = validator or DefinitionValidator(
            models or ModelRegistry(settings=None),
            capability_registry,
            output_registry,
        )

    async def create_draft(self, *, agent_key: str, config: AgentConfig, created_by: str) -> AgentDefinition:
        """Add the next version of an agent as a draft."""
        config = AgentConfig.model_validate(config.model_dump(by_alias=True))
        async with self._session_factory() as session, session.begin():
            # Serialize even the first insert for a key. Released on commit/rollback.
            lock_key = int.from_bytes(sha256(f"asas-agent:draft:{agent_key}".encode()).digest()[:8], signed=True)
            await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
            next_version = (
                await session.scalar(
                    select(func.coalesce(func.max(agent_definitions.c.version), 0) + 1).where(
                        agent_definitions.c.agent_key == agent_key
                    )
                )
            ) or 1

            row = (
                await session.execute(
                    insert(agent_definitions)
                    .values(
                        agent_key=agent_key,
                        version=next_version,
                        status="draft",
                        config=config.model_dump(mode="json", by_alias=True),
                        created_by=created_by,
                    )
                    .returning(agent_definitions)
                )
            ).one()

        return AgentDefinition.from_row(row)

    async def get(self, *, agent_key: str, version: int) -> AgentDefinition:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(agent_definitions).where(
                        agent_definitions.c.agent_key == agent_key,
                        agent_definitions.c.version == version,
                    )
                )
            ).one_or_none()

        if row is None:
            raise RegistryError(f"{agent_key} v{version} does not exist")
        return AgentDefinition.from_row(row)

    async def list_versions(self, agent_key: str | None = None) -> list[AgentDefinition]:
        query = select(agent_definitions).order_by(agent_definitions.c.agent_key, agent_definitions.c.version.desc())
        if agent_key:
            query = query.where(agent_definitions.c.agent_key == agent_key)

        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return [AgentDefinition.from_row(r) for r in rows]

    async def publish(
        self,
        *,
        agent_key: str,
        version: int,
        pinned_config: AgentConfig | None = None,
    ) -> AgentDefinition:
        """Lock a draft, resolving its prompt for every publication entry point."""
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(agent_definitions)
                    .where(
                        agent_definitions.c.agent_key == agent_key,
                        agent_definitions.c.version == version,
                    )
                    .with_for_update()
                )
            ).one_or_none()

            if row is None:
                raise RegistryError(f"{agent_key} v{version} does not exist")
            if row.status == "published":
                raise RegistryError(f"{agent_key} v{version} is already published")
            if row.status == "archived":
                raise RegistryError(f"{agent_key} v{version} is archived and cannot be published")

            config = AgentConfig.model_validate(
                pinned_config.model_dump(by_alias=True) if pinned_config is not None else row.config
            )

            await self._validate_config(agent_key, config)
            config.prompt = await pin_prompt(config.prompt, self._prompts)
            values: dict[str, Any] = {
                "status": "published",
                "published_at": func.now(),
                "config": config.model_dump(mode="json", by_alias=True),
            }

            updated = (
                await session.execute(
                    update(agent_definitions)
                    .where(
                        agent_definitions.c.agent_key == agent_key,
                        agent_definitions.c.version == version,
                    )
                    .values(**values)
                    .returning(agent_definitions)
                )
            ).one()

        return AgentDefinition.from_row(updated)

    async def _validate_config(self, agent_key: str, config: AgentConfig) -> None:
        await self._validator.validate(config, agent_key=agent_key)

    async def bind(self, *, agent_key: str, environment: Environment, version: int, updated_by: str) -> None:
        """Point an environment at a published version. This is promotion and rollback."""
        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(agent_definitions.c.status, agent_definitions.c.config).where(
                        agent_definitions.c.agent_key == agent_key,
                        agent_definitions.c.version == version,
                    )
                )
            ).one_or_none()

            if row is None:
                raise RegistryError(f"{agent_key} v{version} does not exist")
            if row.status != "published":
                raise RegistryError(f"{agent_key} v{version} is {row.status}. Publish it before binding an environment")

            statement = pg_insert(agent_environment_bindings).values(
                agent_key=agent_key,
                environment=environment,
                agent_version=version,
                updated_by=updated_by,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[agent_environment_bindings.c.agent_key, agent_environment_bindings.c.environment],
                    set_={
                        "agent_version": version,
                        "updated_by": updated_by,
                        "updated_at": func.now(),
                    },
                )
            )

    async def release(
        self,
        *,
        agent_key: str,
        config: AgentConfig,
        environment: Environment,
        created_by: str,
    ) -> AgentDefinition:
        """Add a version, publish it, and make it the one this environment runs.

        The three steps an application performs to ship an agent, in the order
        that keeps each one's guarantees: a draft is validated when published,
        and only a published version can be bound to an environment.

        This is a deployment step. It is not idempotent - every call adds a
        version and moves the environment to it - and its three steps commit
        separately, so a definition refused at publication leaves its draft
        behind, and a failure at binding leaves a published version nothing
        runs. Neither is reachable by a request.
        """
        definition = await self.create_draft(agent_key=agent_key, config=config, created_by=created_by)
        published = await self.publish(agent_key=agent_key, version=definition.version)
        await self.bind(
            agent_key=agent_key,
            environment=environment,
            version=definition.version,
            updated_by=created_by,
        )
        return published

    async def get_active(self, *, agent_key: str, environment: Environment) -> AgentDefinition:
        """The version this environment runs. The runtime calls this on every request."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(agent_definitions)
                    .join(
                        agent_environment_bindings,
                        (agent_environment_bindings.c.agent_key == agent_definitions.c.agent_key)
                        & (agent_environment_bindings.c.agent_version == agent_definitions.c.version),
                    )
                    .where(
                        agent_definitions.c.agent_key == agent_key,
                        agent_environment_bindings.c.environment == environment,
                    )
                )
            ).one_or_none()

        if row is None:
            raise RegistryError(f"No agent is bound to {agent_key} in {environment}")
        return AgentDefinition.from_row(row)

    async def bindings(self) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(agent_environment_bindings).order_by(
                        agent_environment_bindings.c.agent_key,
                        agent_environment_bindings.c.environment,
                    )
                )
            ).all()
        return [dict(r._mapping) for r in rows]
