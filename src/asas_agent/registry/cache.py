"""Caching what a run reads before it can do any work.

Every run resolves which version its environment is bound to. A product that
fans out - one call per rubric area, one per candidate in a batch - asks the
same question dozens of times within a second, and would otherwise open dozens
of connections to be told the same thing.

What is cached is the answer to that one question: the version an environment
is bound to, and the published definition it resolves to. A published version
never changes, but the binding does - that is what promotion and rollback are -
so an answer is only good for a few seconds.

Rolling back therefore takes effect within that window, without a deployment,
which is the property the registry exists to provide. A write made through this
process drops what it affects at once, and a query that started before the
write is not allowed to store its answer afterwards. Another process sees the
change when its own entry expires.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from asas_agent.registry.repository import AgentDefinition, AgentRepository
from asas_agent.registry.schema import Environment


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    shared: int = 0  # Runs that waited on another run's query instead of making their own.

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "shared": self.shared}


def _own_copy(definition: AgentDefinition) -> AgentDefinition:
    """Hand each run its own configuration object.

    A cached definition is shared by every run for its lifetime, and
    `AgentConfig` is mutable, so one run editing what it was given would
    change what the others read. Copying costs microseconds against a query
    that costs milliseconds.
    """
    return replace(definition, config=definition.config.model_copy(deep=True))


class _Entry:
    __slots__ = ("definition", "expires_at")

    def __init__(self, definition: AgentDefinition, expires_at: float):
        self.definition = definition
        self.expires_at = expires_at


class CachedAgentRepository:
    """An `AgentRepository` that answers `get_active` from memory when it safely can.

    Everything else - drafts, publication, promotion, listings - goes straight
    to the database, and the writes among them drop what they invalidate.
    """

    def __init__(self, repository: AgentRepository, *, ttl_seconds: float = 5.0, max_entries: int = 256):
        self._repository = repository
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._in_flight: dict[tuple[str, str], asyncio.Task[AgentDefinition]] = {}
        #: Bumped by every write. A query that started before one is not stored.
        self._generation = 0
        self.stats = CacheStats()

    def __getattr__(self, name: str) -> Any:
        """Anything this class does not handle is the repository's own."""
        if name.startswith("_"):
            # Private state is the repository's business, and answering these
            # during construction or a copy would recurse on `_repository`.
            raise AttributeError(name)
        return getattr(self._repository, name)

    async def get_active(self, *, agent_key: str, environment: Environment) -> AgentDefinition:
        key = (agent_key, environment)
        now = asyncio.get_running_loop().time()

        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return _own_copy(entry.definition)

        task = self._in_flight.get(key)
        if task is not None:
            # A run already asked this question; wait for its answer rather
            # than opening another connection to ask it again.
            self.stats.shared += 1
            return _own_copy(await asyncio.shield(task))

        self.stats.misses += 1
        task = asyncio.create_task(self._repository.get_active(agent_key=agent_key, environment=environment))
        self._in_flight[key] = task
        # The result is stored by the query itself, not by whoever started it:
        # a run that hits its deadline and walks away should not throw away an
        # answer its siblings, and the next fan-out, are about to need. This
        # also retrieves the exception of a failed query that nobody awaited.
        task.add_done_callback(partial(self._finished, key, self._generation))

        # Registration is ended by the query finishing, or by a write, never by
        # the caller that happened to start it: `shield` keeps the query running
        # after a deadline, and a run arriving meanwhile should join it rather
        # than open a second one.
        return _own_copy(await asyncio.shield(task))

    def _finished(self, key: tuple[str, str], generation: int, task: asyncio.Task[AgentDefinition]) -> None:
        if self._in_flight.get(key) is task:
            self._in_flight.pop(key, None)
        if task.cancelled() or task.exception() is not None:
            return  # A failure is never remembered; the next run asks again.
        if generation != self._generation:
            # A promotion or rollback landed while this query was running, so
            # what it returned may already be the previous version. Storing it
            # would undo the write for a whole lifetime.
            return
        self._store(key, task.result())

    def _store(self, key: tuple[str, str], definition: AgentDefinition) -> None:
        if self._ttl <= 0:
            return
        expires_at = asyncio.get_running_loop().time() + self._ttl
        self._entries[key] = _Entry(definition, expires_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, agent_key: str | None = None) -> None:
        """Forget one agent, or everything. Called after a write this process made."""
        self._generation += 1
        if agent_key is None:
            self._entries.clear()
            self._in_flight.clear()
            return
        for key in [k for k in self._entries if k[0] == agent_key]:
            del self._entries[key]
        # A run arriving now must not join a query that predates the write.
        for key in [k for k in self._in_flight if k[0] == agent_key]:
            del self._in_flight[key]

    async def publish(self, *, agent_key: str, version: int, **kwargs: Any) -> AgentDefinition:
        definition = await self._repository.publish(agent_key=agent_key, version=version, **kwargs)
        self.invalidate(agent_key)
        return definition

    async def bind(self, *, agent_key: str, environment: Environment, version: int, updated_by: str) -> None:
        await self._repository.bind(
            agent_key=agent_key, environment=environment, version=version, updated_by=updated_by
        )
        self.invalidate(agent_key)
