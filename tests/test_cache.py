"""What a run may reuse, and for how long.

A published definition never changes, so reusing it is free. An environment
binding does change - that is what promotion and rollback are - so reuse of it
is bounded, and a write this process made ends it at once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from asas_agent.integrations.prompts import FilePrompts
from asas_agent.registry.cache import CachedAgentRepository
from asas_agent.registry.repository import RegistryError
from asas_agent.registry.schema import PromptRef


class Registry:
    """A repository that counts what it is asked, and can be repointed."""

    def __init__(self, version: int = 1, delay: float = 0):
        self.version = version
        self.delay = delay
        self.calls = 0
        self.writes: list[tuple[str, ...]] = []

    async def get_active(self, *, agent_key, environment):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.version is None:
            raise RegistryError(f"No agent is bound to {agent_key} in {environment}")
        return SimpleNamespace(agent_key=agent_key, version=self.version, config=None)

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


@pytest.mark.parametrize("write", ["bind", "publish"])
async def test_a_write_here_ends_the_reuse_immediately(write):
    """No one should have to wait out a TTL for a promotion they just made."""
    registry = Registry()
    cached = CachedAgentRepository(registry, ttl_seconds=60)
    await cached.get_active(agent_key="scorer", environment="dev")

    if write == "bind":
        await cached.bind(agent_key="scorer", environment="dev", version=7, updated_by="cli")
    else:
        registry.version = 7
        await cached.publish(agent_key="scorer", version=7)

    assert (await cached.get_active(agent_key="scorer", environment="dev")).version == 7
    assert registry.calls == 2


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
