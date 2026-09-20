"""Caching what a run reads before it can do any work.

Every run resolves which version its environment is bound to. A product that
fans out - one call per rubric area, one per candidate in a batch - asks the
same question dozens of times within a second, and would otherwise open dozens
of connections to be told the same thing.

Two rules shape what is safe to cache:

- A published definition never changes, so its body is cached until evicted.
- An environment binding does change, because that is how promotion and
  rollback work, so it is cached for a few seconds only.

Rolling back therefore takes effect within the binding's lifetime, without a
deployment, which is the property the registry exists to provide. Writes made
through this process clear the entries they affect at once; another process
sees the change when its own entry expires.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
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
        self.stats = CacheStats()

    def __getattr__(self, name: str) -> Any:
        """Anything this class does not handle is the repository's own."""
        return getattr(self._repository, name)

    async def get_active(self, *, agent_key: str, environment: Environment) -> AgentDefinition:
        key = (agent_key, environment)
        now = asyncio.get_running_loop().time()

        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return entry.definition

        task = self._in_flight.get(key)
        if task is not None:
            # A run already asked this question; wait for its answer rather
            # than opening another connection to ask it again.
            self.stats.shared += 1
            return await asyncio.shield(task)

        self.stats.misses += 1
        task = asyncio.create_task(self._repository.get_active(agent_key=agent_key, environment=environment))
        # The query outlives a caller that gives up, so make sure a failure is
        # always retrieved and never surfaces as an unhandled task exception.
        task.add_done_callback(lambda finished: finished.cancelled() or finished.exception())
        self._in_flight[key] = task
        try:
            definition = await asyncio.shield(task)
        finally:
            self._in_flight.pop(key, None)

        self._store(key, definition)
        return definition

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
        if agent_key is None:
            self._entries.clear()
            return
        for key in [k for k in self._entries if k[0] == agent_key]:
            del self._entries[key]

    async def publish(self, *, agent_key: str, version: int, **kwargs: Any) -> AgentDefinition:
        definition = await self._repository.publish(agent_key=agent_key, version=version, **kwargs)
        self.invalidate(agent_key)
        return definition

    async def bind(self, *, agent_key: str, environment: Environment, version: int, updated_by: str) -> None:
        await self._repository.bind(
            agent_key=agent_key, environment=environment, version=version, updated_by=updated_by
        )
        self.invalidate(agent_key)
