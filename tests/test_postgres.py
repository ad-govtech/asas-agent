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
from asas_agent.registry.outputs import OutputSchemaError, OutputSchemaRegistry
from asas_agent.registry.repository import AgentRepository
from asas_agent.registry.schema import AgentConfig
from asas_agent.registry.validation import DefinitionValidator

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
    validator = DefinitionValidator(ModelRegistry(None), CapabilityRegistry(), OutputSchemaRegistry())
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
        ({"model": {"provider": "unknown", "name": "model"}}, ModelError),
        (
            {"model": {"provider": "openai", "name": "gpt-5-nano", "settings": {"reasoning_effort": "invalid"}}},
            ModelError,
        ),
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


async def test_release_publishes_and_makes_it_live_in_one_call(repository):
    """The three steps every application was writing for itself."""
    definition = await repository.release(agent_key="released", config=config(), environment="dev", created_by="test")

    assert definition.status == "published"
    assert (await repository.get_active(agent_key="released", environment="dev")).version == definition.version


async def test_release_refuses_a_definition_that_could_not_run(repository):
    with pytest.raises(CapabilityError):
        await repository.release(
            agent_key="released", config=config(tools=["unknown"]), environment="dev", created_by="test"
        )

    # Nothing left behind: no binding, and the draft is still a draft.
    assert not await repository.bindings()
    assert (await repository.get(agent_key="released", version=1)).status == "draft"


async def test_release_again_adds_the_next_version_and_moves_the_environment(repository):
    first = await repository.release(agent_key="released", config=config(), environment="dev", created_by="test")
    second = await repository.release(
        agent_key="released", config=config(description="second"), environment="dev", created_by="test"
    )

    assert (first.version, second.version) == (1, 2)
    assert (await repository.get_active(agent_key="released", environment="dev")).version == 2
