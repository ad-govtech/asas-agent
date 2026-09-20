"""What a run may reuse, and for how long.

A published definition never changes, so reusing it is free. An environment
binding does change - that is what promotion and rollback are - so reuse of it
is bounded, and a write this process made ends it at once.
"""

from __future__ import annotations

import asyncio
import copy
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from asas_agent.integrations.prompts import FilePrompts
from asas_agent.registry.cache import CachedAgentRepository
from asas_agent.registry.repository import AgentDefinition, RegistryError
from asas_agent.registry.schema import AgentConfig, PromptRef


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

    def __init__(self, version: int = 1, delay: float = 0):
        self.version = version
        self.delay = delay
        self.calls = 0
        self.writes: list[tuple[str, ...]] = []

    async def get_active(self, *, agent_key, environment):
        self.calls += 1
        # A real query reads the row when it runs, then takes time to come
        # back, so a write landing meanwhile does not change what it returns.
        version = self.version
        if self.delay:
            await asyncio.sleep(self.delay)
        if version is None:
            raise RegistryError(f"No agent is bound to {agent_key} in {environment}")
        return definition(agent_key, version)

    async def bind(self, *, agent_key, environment, version, updated_by):
        self.writes.append(("bind", agent_key))
        self.version = version

    async def publish(self, *, agent_key, version, **kwargs):
        self.writes.append(("publish", agent_key))
        return SimpleNamespace(agent_key=agent_key, version=version)

    async def bindings(self):
        return [{"agent_key": "scorer", "environment": "dev", "agent_version": self.version}]


async def test_a_second_run_reuses_the_first_ones_answer():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    for _ in range(5):
        assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 1

    assert registry.calls == 1
    assert cached.stats.as_dict() == {"hits": 4, "misses": 1, "shared": 0}


async def test_a_fan_out_asks_the_registry_once_not_once_per_branch():
    """Scoring a batch starts dozens of runs at once; they need one answer between them."""
    registry = Registry(delay=0.02)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    results = await asyncio.gather(*(cached.get_active(agent_key="scorer", environment="dev") for _ in range(40)))

    assert {r.version for r in results} == {1}
    assert registry.calls == 1
    assert cached.stats.shared == 39


async def test_each_agent_and_environment_is_cached_separately():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    await cached.get_active(agent_key="scorer", environment="dev")
    await cached.get_active(agent_key="scorer", environment="production")
    await cached.get_active(agent_key="planner", environment="dev")
    await cached.get_active(agent_key="scorer", environment="dev")

    assert registry.calls == 3


async def test_a_rollback_reaches_a_running_process_when_the_entry_expires():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=0.05)

    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 1
    registry.version = 7  # Another process rolled production back.
    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 1

    await asyncio.sleep(0.06)
    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 7


async def test_a_promotion_here_ends_the_reuse_immediately():
    """No one should have to wait out a window for a promotion they just made."""
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)
    await cached.get_active(agent_key="scorer", environment="dev")

    await cached.bind(agent_key="scorer", environment="dev", version=7, updated_by="cli")

    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 7
    assert registry.calls == 2


async def test_publishing_also_drops_what_was_remembered():
    """Publishing cannot move a binding on its own, but it is a write and must not be trusted to be harmless."""
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)
    await cached.get_active(agent_key="scorer", environment="dev")

    await cached.publish(agent_key="scorer", version=7)

    await cached.get_active(agent_key="scorer", environment="dev")
    assert registry.calls == 2  # Asked again rather than answered from memory.


async def test_a_write_to_one_agent_leaves_the_others_alone():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)
    await cached.get_active(agent_key="scorer", environment="dev")
    await cached.get_active(agent_key="planner", environment="dev")

    await cached.bind(agent_key="scorer", environment="dev", version=7, updated_by="cli")

    await cached.get_active(agent_key="planner", environment="dev")
    assert registry.calls == 2  # planner still answered from memory


async def test_nothing_is_cached_when_the_ttl_is_zero():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=0)

    await cached.get_active(agent_key="scorer", environment="dev")
    await cached.get_active(agent_key="scorer", environment="dev")

    assert registry.calls == 2


async def test_the_cache_stays_within_its_size():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60, max_entries=2)

    for key in ("a", "b", "c"):
        await cached.get_active(agent_key=key, environment="dev")
    await cached.get_active(agent_key="a", environment="dev")  # Evicted by "c".

    assert registry.calls == 4
    assert len(cached._entries) == 2


async def test_a_failure_is_not_remembered_and_does_not_escape():
    registry = Registry(version=None)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    for _ in range(2):
        with pytest.raises(RegistryError):
            await cached.get_active(agent_key="scorer", environment="dev")

    assert registry.calls == 2


async def test_a_caller_that_gives_up_does_not_take_the_answer_with_it():
    """A run hitting its deadline must not cancel the query its siblings are waiting on."""
    registry = Registry(delay=0.05)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    first = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0)
    second = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert (await second).version == 1
    assert registry.calls == 1


async def test_everything_else_is_still_the_repositorys_own():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)
    assert (await cached.bindings())[0]["agent_key"] == "scorer"


# ----- prompt files -------------------------------------------------------------------


async def test_a_prompt_file_is_read_once_and_again_after_it_changes(tmp_path):
    path = tmp_path / "advisor.md"
    path.write_text("First", encoding="utf-8")
    provider = FilePrompts(tmp_path)
    reads = []

    original = provider._read
    provider._read = lambda p: (reads.append(p), original(p))[1]

    for _ in range(3):
        assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "First"
    assert len(reads) == 1

    path.write_text("Second", encoding="utf-8")
    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "Second"
    assert len(reads) == 2


async def test_a_prompt_file_that_becomes_a_chat_prompt_is_picked_up(tmp_path):
    (tmp_path / "advisor.md").write_text("Text version", encoding="utf-8")
    provider = FilePrompts(tmp_path)
    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "Text version"

    (tmp_path / "advisor.chat.json").write_text('[{"role": "system", "content": "Chat version"}]')
    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "Chat version"


async def test_caching_can_be_turned_off_for_a_prompt_folder(tmp_path):
    path = tmp_path / "advisor.md"
    path.write_text("First", encoding="utf-8")
    provider = FilePrompts(tmp_path, cache=False)
    reads = []
    original = provider._read
    provider._read = lambda p: (reads.append(p), original(p))[1]

    await provider.resolve(PromptRef(name="advisor"))
    await provider.resolve(PromptRef(name="advisor"))
    assert len(reads) == 2


# ----- a write racing a query ----------------------------------------------------------


async def test_a_rollback_is_not_undone_by_a_query_that_started_before_it():
    """The query holds the pre-rollback answer. Storing it would revive the version just rolled back."""
    registry = Registry(version=2, delay=0.05)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    in_flight = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0.01)  # The query has really started and holds v2.

    await cached.bind(agent_key="scorer", environment="dev", version=1, updated_by="cli")
    assert (await in_flight).version == 2  # This run started earlier and keeps its answer.

    # Every run after the rollback sees the rollback.
    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 1


async def test_a_run_arriving_after_a_rollback_does_not_join_the_query_that_predates_it():
    registry = Registry(version=2, delay=0.05)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    in_flight = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0.01)
    await cached.bind(agent_key="scorer", environment="dev", version=1, updated_by="cli")

    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 1
    await in_flight


async def test_an_answer_outlives_the_run_that_gave_up_waiting_for_it():
    """A deadline is the normal way a run ends; the next one should not pay for the query again."""
    registry = Registry(delay=0.05)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    impatient = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0.01)
    impatient.cancel()
    with pytest.raises(asyncio.CancelledError):
        await impatient

    await asyncio.sleep(0.06)  # The query finishes on its own and keeps its answer.
    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 1
    assert registry.calls == 1


async def test_a_waiter_that_gives_up_does_not_cancel_the_query_for_the_others():
    registry = Registry(delay=0.05)
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    first = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(cached.get_active(agent_key="scorer", environment="dev"))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert (await first).version == 1
    assert registry.calls == 1


# ----- what a run is handed ------------------------------------------------------------


async def test_each_run_gets_its_own_configuration_to_work_with():
    """A shared definition would let one run's edit change what another run reads."""
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    first = await cached.get_active(agent_key="scorer", environment="dev")
    second = await cached.get_active(agent_key="scorer", environment="dev")

    assert first.config is not second.config
    first.config.tools.append("policy.search")
    assert second.config.tools == []
    assert (await cached.get_active(agent_key="scorer", environment="dev")).config.tools == []


async def test_a_recent_hit_is_not_the_first_thing_evicted():
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60, max_entries=2)

    await cached.get_active(agent_key="a", environment="dev")
    await cached.get_active(agent_key="b", environment="dev")
    await cached.get_active(agent_key="a", environment="dev")  # "a" is used again.
    await cached.get_active(agent_key="c", environment="dev")  # Evicts "b", the idle one.

    await cached.get_active(agent_key="a", environment="dev")
    assert registry.calls == 3


async def test_private_repository_state_stays_private():
    """The wrapper delegates an interface, not the object's insides."""
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)

    assert not hasattr(cached, "_session_factory")
    assert copy.deepcopy(cached.stats) == cached.stats  # No recursion through __getattr__.


# ----- prompt files -------------------------------------------------------------------


async def test_a_prompt_edited_while_it_is_read_is_not_cached(tmp_path):
    """Otherwise the text we did not get is stored under the stamp of the text we did not read."""
    path = tmp_path / "advisor.md"
    path.write_text("OLD", encoding="utf-8")
    provider = FilePrompts(tmp_path)

    original = provider._read

    def read_then_edit(p):
        content = original(p)
        path.write_text("NEW", encoding="utf-8")  # A writer lands during the read.
        return content

    provider._read = read_then_edit
    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "OLD"

    provider._read = original
    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "NEW"


async def test_a_prompt_of_the_same_age_and_a_different_size_is_read_again(tmp_path):
    path = tmp_path / "advisor.md"
    path.write_text("First", encoding="utf-8")
    provider = FilePrompts(tmp_path)
    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "First"

    stamp = path.stat()
    path.write_text("First and more", encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # Same age, different size.

    assert (await provider.resolve(PromptRef(name="advisor"))).instructions == "First and more"


async def test_two_prompt_folders_do_not_share_a_name(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "one" / "advisor.md").write_text("From one", encoding="utf-8")
    (tmp_path / "two" / "advisor.md").write_text("From two", encoding="utf-8")

    assert (await FilePrompts(tmp_path / "one").resolve(PromptRef(name="advisor"))).instructions == "From one"
    assert (await FilePrompts(tmp_path / "two").resolve(PromptRef(name="advisor"))).instructions == "From two"


# ----- wiring -------------------------------------------------------------------------


async def test_a_platform_runs_its_agents_through_the_cache(monkeypatch, tmp_path):
    """Every other test patches the repository's own method, which hides the wrapper."""
    from unittest.mock import AsyncMock

    from agents import Runner

    from asas_agent.bootstrap import build_platform
    from asas_agent.config import Settings
    from asas_agent.registry.cache import CachedAgentRepository
    from asas_agent.runtime.context import RuntimeContext

    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql://test@localhost/test",
        OPENAI_API_KEY="test",
        ASAS_DEFINITION_CACHE_SECONDS=30,
        ASAS_DEFINITION_CACHE_SIZE=7,
    )
    platform = build_platform(settings)
    try:
        assert isinstance(platform.repository, CachedAgentRepository)
        assert (platform.repository._ttl, platform.repository._max_entries) == (30, 7)

        query = AsyncMock(return_value=definition("scorer", 3))
        monkeypatch.setattr(platform.repository._repository, "get_active", query)
        monkeypatch.setattr(Runner, "run", AsyncMock(return_value=SimpleNamespace(final_output="done")))

        for _ in range(4):
            result = await platform.runtime.run(
                agent_key="scorer",
                environment="dev",
                user_input="hi",
                context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
            )
            assert result.agent_version == 3

        assert query.await_count == 1
    finally:
        await platform.close()
