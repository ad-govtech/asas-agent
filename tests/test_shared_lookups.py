"""What runs share, and what they do not.

Runs that ask the same question at the same moment share one query. Nothing is
kept afterwards, so a promotion is visible to the next run and there is no
staleness to reason about.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from asas_agent.registry.lookups import SharedAgentLookups
from asas_agent.registry.repository import AgentDefinition, RegistryError
from asas_agent.registry.schema import AgentConfig


def definition(agent_key: str, version: int) -> AgentDefinition:
    """What the repository really returns, so the tests meet a real AgentConfig."""
    return AgentDefinition(
        agent_key=agent_key,
        version=version,
        status="published",
        config=AgentConfig.model_validate(
            {
                "name": "Scorer",
                "prompt": {"name": "scorer", "snapshot": "You score."},
                "model": {"provider": "openai", "name": "gpt-5-nano"},
            }
        ),
        created_at=datetime.now(UTC),
        created_by="test",
        published_at=datetime.now(UTC),
    )


class Registry:
    """A repository that counts what it is asked, and can be repointed."""

    def __init__(self, version: int | None = 1, delay: float = 0):
        self.version = version
        self.delay = delay
        self.calls = 0

    async def get_active(self, *, agent_key, environment):
        self.calls += 1
        # A real query reads the row when it runs, then takes time to come back.
        version = self.version
        if self.delay:
            await asyncio.sleep(self.delay)
        if version is None:
            raise RegistryError(f"No agent is bound to {agent_key} in {environment}")
        return definition(agent_key, version)

    async def bind(self, *, agent_key, environment, version, updated_by):
        self.version = version

    async def publish(self, *, agent_key, version, **kwargs):
        return definition(agent_key, version)

    async def bindings(self):
        return [{"agent_key": "scorer", "environment": "dev", "agent_version": self.version}]


async def test_a_fan_out_asks_once_not_once_per_branch():
    """Scoring a batch starts dozens of runs at once; they need one answer between them."""
    registry = Registry(delay=0.02)
    shared = SharedAgentLookups(registry)

    results = await asyncio.gather(*(shared.get_active(agent_key="scorer", environment="dev") for _ in range(40)))

    assert {r.version for r in results} == {1}
    assert registry.calls == 1
    assert shared.stats.as_dict() == {"queries": 1, "shared": 39}


async def test_runs_that_do_not_overlap_each_ask():
    """Nothing is kept, so nothing can be stale."""
    registry = Registry()
    shared = SharedAgentLookups(registry)

    await shared.get_active(agent_key="scorer", environment="dev")
    await shared.get_active(agent_key="scorer", environment="dev")

    assert registry.calls == 2


async def test_a_promotion_is_visible_to_the_very_next_run():
    registry = Registry()
    shared = SharedAgentLookups(registry)

    assert (await shared.get_active(agent_key="scorer", environment="dev")).version == 1
    registry.version = 7  # Another process promoted.
    assert (await shared.get_active(agent_key="scorer", environment="dev")).version == 7


async def test_each_question_is_shared_separately():
    registry = Registry(delay=0.02)
    shared = SharedAgentLookups(registry)

    await asyncio.gather(
        shared.get_active(agent_key="scorer", environment="dev"),
        shared.get_active(agent_key="scorer", environment="production"),
        shared.get_active(agent_key="planner", environment="dev"),
        shared.get_active(agent_key="scorer", environment="dev"),
    )

    assert registry.calls == 3


# ----- a write made here ---------------------------------------------------------------


async def test_a_promotion_here_detaches_the_query_it_affects():
    """A run arriving after the write must not join a query that read the registry before it."""
    registry = Registry(version=2, delay=0.05)
    shared = SharedAgentLookups(registry)

    in_flight = asyncio.create_task(shared.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0.01)  # The query has started and holds v2.

    await shared.bind(agent_key="scorer", environment="dev", version=1, updated_by="cli")

    assert (await shared.get_active(agent_key="scorer", environment="dev")).version == 1
    assert (await in_flight).version == 2  # This run asked first and keeps its answer.
    assert registry.calls == 2


async def test_publishing_detaches_too():
    registry = Registry(delay=0.05)
    shared = SharedAgentLookups(registry)

    in_flight = asyncio.create_task(shared.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0.01)
    await shared.publish(agent_key="scorer", version=2)
    registry.version = 2

    assert (await shared.get_active(agent_key="scorer", environment="dev")).version == 2
    await in_flight


async def test_a_write_to_one_agent_leaves_another_agents_query_shared():
    registry = Registry(delay=0.05)
    shared = SharedAgentLookups(registry)

    planner = asyncio.create_task(shared.get_active(agent_key="planner", environment="dev"))
    await asyncio.sleep(0.01)
    await shared.bind(agent_key="scorer", environment="dev", version=7, updated_by="cli")

    joined = asyncio.create_task(shared.get_active(agent_key="planner", environment="dev"))
    await asyncio.gather(planner, joined)
    assert registry.calls == 1


# ----- runs that give up ---------------------------------------------------------------


async def test_a_waiting_run_that_gives_up_leaves_the_query_alone():
    registry = Registry(delay=0.05)
    shared = SharedAgentLookups(registry)

    first = asyncio.create_task(shared.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(shared.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert (await first).version == 1
    assert registry.calls == 1


async def test_a_run_arriving_after_the_first_gave_up_joins_the_query_it_left_running():
    """The query is still open; a second one would be pure waste."""
    registry = Registry(delay=0.05)
    shared = SharedAgentLookups(registry)

    impatient = asyncio.create_task(shared.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0.01)
    impatient.cancel()
    with pytest.raises(asyncio.CancelledError):
        await impatient

    assert (await shared.get_active(agent_key="scorer", environment="dev")).version == 1
    assert registry.calls == 1


async def test_repeated_deadlines_do_not_pile_up_queries():
    """A slow database and short deadlines is when a second query hurts most."""
    registry = Registry(delay=0.2)
    shared = SharedAgentLookups(registry)

    for _ in range(5):
        wave = [asyncio.create_task(shared.get_active(agent_key="scorer", environment="dev")) for _ in range(4)]
        await asyncio.sleep(0.01)
        for task in wave:
            task.cancel()
        await asyncio.gather(*wave, return_exceptions=True)

    assert registry.calls == 1


# ----- what a run is handed ------------------------------------------------------------


async def test_runs_sharing_a_query_do_not_share_its_configuration():
    registry = Registry(delay=0.01)
    shared = SharedAgentLookups(registry)

    first, second = await asyncio.gather(
        shared.get_active(agent_key="scorer", environment="dev"),
        shared.get_active(agent_key="scorer", environment="dev"),
    )

    assert first.config is not second.config
    first.config.tools.append("policy.search")
    assert second.config.tools == []


async def test_a_failure_reaches_every_run_waiting_and_is_not_remembered():
    registry = Registry(version=None, delay=0.01)
    shared = SharedAgentLookups(registry)

    with pytest.raises(RegistryError):
        await asyncio.gather(
            shared.get_active(agent_key="scorer", environment="dev"),
            shared.get_active(agent_key="scorer", environment="dev"),
        )

    registry.version = 3
    assert (await shared.get_active(agent_key="scorer", environment="dev")).version == 3
    assert registry.calls == 2  # The failed query was shared, then asked again.


async def test_nothing_is_left_registered_afterwards():
    registry = Registry()
    shared = SharedAgentLookups(registry)
    await shared.get_active(agent_key="scorer", environment="dev")
    assert shared._in_flight == {}


async def test_everything_else_is_still_the_repositorys_own():
    shared = SharedAgentLookups(Registry())
    assert (await shared.bindings())[0]["agent_key"] == "scorer"


async def test_private_repository_state_stays_private():
    shared = SharedAgentLookups(Registry())
    assert not hasattr(shared, "_session_factory")
    assert copy.deepcopy(shared.stats) == shared.stats  # No recursion through __getattr__.


async def test_a_platform_runs_its_agents_through_the_shared_lookup(monkeypatch):
    """Every other runtime test patches the repository's own method, which hides the wrapper."""
    from agents import Runner

    from asas_agent.bootstrap import build_platform
    from asas_agent.config import Settings
    from asas_agent.runtime.context import RuntimeContext

    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    settings = Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test", OPENAI_API_KEY="test")
    platform = build_platform(settings)
    try:
        assert isinstance(platform.repository, SharedAgentLookups)
        query = AsyncMock(return_value=definition("scorer", 3))
        monkeypatch.setattr(platform.repository._repository, "get_active", query)
        monkeypatch.setattr(Runner, "run", AsyncMock(return_value=SimpleNamespace(final_output="done")))

        results = await asyncio.gather(
            *(
                platform.runtime.run(
                    agent_key="scorer",
                    environment="dev",
                    message="hi",
                    context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
                )
                for _ in range(4)
            )
        )
        assert {r.agent_version for r in results} == {3}
        assert query.await_count == 1
    finally:
        await platform.close()
