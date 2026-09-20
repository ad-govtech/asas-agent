"""Opt-in tests against a disposable PostgreSQL database, isolated by schema."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from asas_agent.integrations.models import ModelError, ModelRegistry
from asas_agent.registry.capabilities import CapabilityError, CapabilityRegistry
from asas_agent.registry.db import metadata
from asas_agent.registry.guardrails import GuardrailError, GuardrailRegistry
from asas_agent.registry.outputs import OutputSchemaError, OutputSchemaRegistry
from asas_agent.registry.repository import AgentRepository, RegistryError
from asas_agent.registry.schema import AgentConfig
from asas_agent.registry.validation import DefinitionError, DefinitionValidator

pytestmark = pytest.mark.skipif(
    not os.getenv("ASAS_TEST_DATABASE_URL"), reason="Set ASAS_TEST_DATABASE_URL for PostgreSQL tests"
)


@pytest.fixture
async def repository():
    engine = create_async_engine(os.environ["ASAS_TEST_DATABASE_URL"])
    schema = "review_" + uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    async with scoped.begin() as connection:
        await connection.run_sync(metadata.create_all)
    validator = DefinitionValidator(
        ModelRegistry(None), CapabilityRegistry(), OutputSchemaRegistry(), GuardrailRegistry()
    )
    try:
        yield AgentRepository(async_sessionmaker(scoped, expire_on_commit=False), validator=validator)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


def config(**overrides):
    return AgentConfig.model_validate(
        {
            "name": "Test",
            "prompt": {"name": "test", "snapshot": "text"},
            "model": {"provider": "openai", "name": "gpt-5-nano"},
            **overrides,
        }
    )


async def release(repository, key, definition=None, promote=True):
    draft = await repository.create_draft(agent_key=key, config=definition or config(), created_by="test")
    await repository.publish(agent_key=key, version=draft.version)
    if promote:
        await repository.bind(agent_key=key, version=draft.version, environment="dev", updated_by="test")
    return draft.version


async def test_concurrent_first_drafts_receive_distinct_versions(repository):
    drafts = await asyncio.gather(
        *[repository.create_draft(agent_key="same", config=config(), created_by="test") for _ in range(20)]
    )
    assert sorted(d.version for d in drafts) == list(range(1, 21))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"tools": ["unknown"]}, CapabilityError),
        ({"output": {"schema": "unknown"}}, OutputSchemaError),
        ({"guardrails": ["unknown"]}, GuardrailError),
        ({"model": {"provider": "unknown", "name": "model"}}, ModelError),
        ({"model": {"provider": "gateway", "name": "jais"}, "tools": ["tool"]}, ModelError),
        (
            {"model": {"provider": "openai", "name": "gpt-5-nano", "settings": {"reasoning_effort": "invalid"}}},
            ModelError,
        ),
        ({"sub_agents": [{"agent_key": "missing", "mode": "tool", "environment": "dev"}]}, RegistryError),
        ({"sub_agents": [{"agent_key": "test", "mode": "tool", "environment": "dev"}]}, DefinitionError),
    ],
)
async def test_invalid_publications_roll_back(repository, overrides, error):
    draft = await repository.create_draft(agent_key="test", config=config(**overrides), created_by="test")
    with pytest.raises(error):
        await repository.publish(agent_key="test", version=draft.version)
    assert (await repository.get(agent_key="test", version=draft.version)).status == "draft"
    assert not await repository.bindings()


async def test_publication_checks_tools_without_executing_them(repository):
    def never_called(context):
        raise AssertionError("Publication executed an action")

    repository._validator.capabilities.capability("action", risk="action")(never_called)
    await release(repository, "action-agent", config(tools=["action"]))
    assert (await repository.get_active(agent_key="action-agent", environment="dev")).status == "published"


async def test_promotion_revalidates_graph_after_publication(repository):
    await release(repository, "a")
    await release(repository, "b")
    a2 = await release(
        repository, "a", config(sub_agents=[{"agent_key": "b", "environment": "dev", "mode": "tool"}]), False
    )
    b2 = await release(
        repository, "b", config(sub_agents=[{"agent_key": "a", "environment": "dev", "mode": "tool"}]), False
    )
    await repository.bind(agent_key="a", version=a2, environment="dev", updated_by="test")
    with pytest.raises(DefinitionError, match="loop"):
        await repository.bind(agent_key="b", version=b2, environment="dev", updated_by="test")
    assert (await repository.get_active(agent_key="b", environment="dev")).version == 1


async def test_one_agent_in_two_environments_is_not_a_loop(repository):
    """`advisor:production -> reviewer:production -> advisor:staging` ends at a different binding."""
    leaf = await release(repository, "advisor", promote=False)
    await repository.bind(agent_key="advisor", version=leaf, environment="staging", updated_by="test")

    reviewer = await release(
        repository,
        "reviewer",
        config(sub_agents=[{"agent_key": "advisor", "environment": "staging", "mode": "tool"}]),
        promote=False,
    )
    await repository.bind(agent_key="reviewer", version=reviewer, environment="production", updated_by="test")

    advisor2 = await release(
        repository,
        "advisor",
        config(sub_agents=[{"agent_key": "reviewer", "environment": "production", "mode": "tool"}]),
        promote=False,
    )
    await repository.bind(agent_key="advisor", version=advisor2, environment="production", updated_by="test")

    assert (await repository.get_active(agent_key="advisor", environment="production")).version == advisor2
    assert (await repository.get_active(agent_key="advisor", environment="staging")).version == leaf


async def test_a_loop_that_runs_through_another_environment_is_still_a_loop(repository):
    """`advisor:staging -> reviewer:production -> advisor:staging` never ends."""
    leaf = await release(repository, "advisor", promote=False)
    await repository.bind(agent_key="advisor", version=leaf, environment="staging", updated_by="test")

    reviewer = await release(
        repository,
        "reviewer",
        config(sub_agents=[{"agent_key": "advisor", "environment": "staging", "mode": "tool"}]),
        promote=False,
    )
    await repository.bind(agent_key="reviewer", version=reviewer, environment="production", updated_by="test")

    advisor2 = await release(
        repository,
        "advisor",
        config(sub_agents=[{"agent_key": "reviewer", "environment": "production", "mode": "tool"}]),
        promote=False,
    )
    with pytest.raises(DefinitionError, match="loop at advisor:staging"):
        await repository.bind(agent_key="advisor", version=advisor2, environment="staging", updated_by="test")

    assert (await repository.get_active(agent_key="advisor", environment="staging")).version == leaf
