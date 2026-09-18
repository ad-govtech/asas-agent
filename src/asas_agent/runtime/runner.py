"""Running an agent, with the trace metadata that makes a run reproducible."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.factory import AgentFactory


@dataclass
class AgentRunResult:
    output: Any
    agent_key: str
    agent_version: int
    prompt_version: int | None
    trace_id: str | None
    toolset: list[str]


class AgentRuntime:
    def __init__(self, factory: AgentFactory, *, tracer=None):
        self.factory = factory
        self._tracer = tracer

    async def run(
        self,
        *,
        agent_key: str,
        environment: str,
        user_input: str,
        business_context: dict[str, Any] | None = None,
        context: RuntimeContext,
    ) -> AgentRunResult:
        from agents import Runner

        built = await self.factory.build(agent_key=agent_key, environment=environment, context=context)

        payload = {"request": user_input}
        if business_context:
            payload["context"] = business_context

        max_turns = min(context.max_turns, built.config.runtime.max_turns)

        with self._trace(agent_key, context) as trace_id:
            result = await Runner.run(
                built.agent,
                input=json.dumps(payload, ensure_ascii=False, default=str),
                context=context,
                max_turns=max_turns,
            )

        return AgentRunResult(
            output=result.final_output,
            agent_key=agent_key,
            agent_version=built.agent_version,
            prompt_version=built.prompt_version,
            trace_id=trace_id,
            toolset=list(built.config.tools),
        )

    def _trace(self, agent_key: str, context: RuntimeContext):
        """Open a Langfuse span when tracing is on; otherwise do nothing."""
        from contextlib import contextmanager

        tracer = self._tracer

        @contextmanager
        def span():
            if tracer is None:
                yield None
                return
            with tracer.start_as_current_observation(name=f"agent:{agent_key}") as observation:
                tracer.update_current_trace(
                    user_id=context.user_id,
                    session_id=context.correlation_id,
                    metadata={**context.trace_metadata, "tenant_id": context.tenant_id},
                    tags=[f"env:{context.environment}", f"agent:{agent_key}"],
                )
                try:
                    yield getattr(observation, "trace_id", None)
                finally:
                    tracer.flush()

        return span()
