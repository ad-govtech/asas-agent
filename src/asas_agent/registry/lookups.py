"""Sharing the lookup every run starts with.

A run cannot begin until it knows which version its environment is bound to. A
product that fans out - one run per rubric area, one per candidate in a batch -
asks that same question dozens of times in the same second, and a pool of five
connections makes them queue: measured against a local Postgres, forty
concurrent lookups take 128 ms where one takes 2.2 ms.

Runs that ask at the same moment therefore share one query. Nothing is stored
afterwards, so a promotion is visible to the next run that asks.

One window is narrower than that: a query already in flight is shared with runs
that arrive while it is open, and it read the registry when it started. A
promotion made through this process detaches that query, so runs arriving after
it ask again. A promotion made somewhere else - another process, the CLI -
cannot, so a run arriving in the moments before that query returns may still be
given the version that was live when it started. The window is one query, and
the run after it is current.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from asas_agent.registry.repository import AgentDefinition, AgentRepository
from asas_agent.registry.schema import Environment


@dataclass
class LookupStats:
    queries: int = 0
    shared: int = 0  # Runs that waited on another run's query instead of making their own.

    def as_dict(self) -> dict[str, int]:
        return {"queries": self.queries, "shared": self.shared}


def _own_copy(definition: AgentDefinition) -> AgentDefinition:
    """Hand each run its own configuration object.

    Runs sharing a query share the object it returns, and `AgentConfig` is
    mutable, so one run editing what it was given would change what the others
    read. Copying costs microseconds against a query that costs milliseconds.
    """
    return replace(definition, config=definition.config.model_copy(deep=True))


class SharedAgentLookups:
    """An `AgentRepository` whose `get_active` is asked once per moment, not once per run.

    Everything else - drafts, publication, promotion, listings - goes straight
    to the repository.
    """

    def __init__(self, repository: AgentRepository):
        self._repository = repository
        self._in_flight: dict[tuple[str, str], asyncio.Task[AgentDefinition]] = {}
        self.stats = LookupStats()

    def __getattr__(self, name: str) -> Any:
        """Anything this class does not handle is the repository's own."""
        if name.startswith("_"):
            # Private state is the repository's business, and answering these
            # during construction or a copy would recurse on `_repository`.
            raise AttributeError(name)
        return getattr(self._repository, name)

    async def get_active(self, *, agent_key: str, environment: Environment) -> AgentDefinition:
        key = (agent_key, environment)

        task = self._in_flight.get(key)
        if task is not None:
            # This question is already being asked; wait for that answer rather
            # than opening another connection to ask it again.
            self.stats.shared += 1
            return _own_copy(await asyncio.shield(task))

        self.stats.queries += 1
        task = asyncio.create_task(self._repository.get_active(agent_key=agent_key, environment=environment))
        self._in_flight[key] = task
        # The query outlives the run that started it: a run that hits its
        # deadline and walks away must not cancel what its siblings are waiting
        # on, and a failure nobody awaits still has to be retrieved.
        task.add_done_callback(partial(self._finished, key))

        return _own_copy(await asyncio.shield(task))

    def _finished(self, key: tuple[str, str], task: asyncio.Task[AgentDefinition]) -> None:
        if self._in_flight.get(key) is task:
            del self._in_flight[key]
        if not task.cancelled():
            task.exception()  # Retrieved, so a failed query is never reported as unhandled.

    def detach(self, agent_key: str | None = None) -> None:
        """Stop sharing the queries in flight for an agent, or for all of them.

        Called after a write this process made: a run arriving now must ask
        again rather than join a query that read the registry before the write.
        Runs already waiting keep the answer they asked for.
        """
        if agent_key is None:
            self._in_flight.clear()
            return
        for key in [k for k in self._in_flight if k[0] == agent_key]:
            del self._in_flight[key]

    async def publish(self, *, agent_key: str, version: int, **kwargs: Any) -> AgentDefinition:
        definition = await self._repository.publish(agent_key=agent_key, version=version, **kwargs)
        self.detach(agent_key)
        return definition

    async def bind(self, *, agent_key: str, environment: Environment, version: int, updated_by: str) -> None:
        await self._repository.bind(
            agent_key=agent_key, environment=environment, version=version, updated_by=updated_by
        )
        self.detach(agent_key)
