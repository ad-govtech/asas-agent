"""Running an agent, with the trace metadata that makes a run reproducible."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from asas_agent.integrations.prompts import PromptMessage
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.factory import AgentFactory


def _payload(user_input: str, business_context: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"request": user_input}
    if business_context:
        payload["context"] = business_context
    return payload


def _run_input(
    prompt_messages: tuple[PromptMessage, ...],
    user_input: str,
    business_context: dict[str, Any] | None,
) -> Any:
    """What the run starts from.

    A text prompt keeps the original contract: one JSON message holding the
    request and the context the calling service loaded. A chat prompt sends its
    own messages first, because the prompt author decided where each fact goes,
    and the JSON message follows only when the caller passed something.
    """
    payload = _payload(user_input, business_context)

    if not prompt_messages:
        return json.dumps(payload, ensure_ascii=False, default=str)

    items: list[dict[str, str]] = [{"role": m.role, "content": m.content} for m in prompt_messages]
    if user_input or business_context:
        items.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)})
    return items


@dataclass
class AgentRunResult:
    output: Any
    agent_key: str
    agent_version: int
    prompt_version: int | None
    trace_id: str | None
    toolset: list[str]


class AgentRuntime:
    def __init__(
        self,
        factory: AgentFactory,
        *,
        tracer=None,
        max_turns_ceiling: int = 20,
        timeout_ceiling_seconds: float = 300,
        dependencies: dict[str, Any] | None = None,
    ):
        self.factory = factory
        self._tracer = tracer
        self.max_turns_ceiling = max_turns_ceiling
        self.timeout_ceiling_seconds = timeout_ceiling_seconds
        self.default_dependencies = dict(dependencies or {})

    async def run(
        self,
        *,
        agent_key: str,
        environment: str,
        user_input: str = "",
        business_context: dict[str, Any] | None = None,
        prompt_variables: dict[str, Any] | None = None,
        context: RuntimeContext,
    ) -> AgentRunResult:
        from agents import RunConfig, Runner

        if context.max_turns < 1 or context.timeout_seconds <= 0:
            raise ValueError("Runtime turn and timeout limits must be positive")
        context = replace(
            context,
            environment=environment,
            max_turns=min(context.max_turns, self.max_turns_ceiling),
            timeout_seconds=min(context.timeout_seconds, self.timeout_ceiling_seconds),
            dependencies={**self.default_dependencies, **context.dependencies},
            trace_metadata=dict(context.trace_metadata),
        )
        started = asyncio.get_running_loop().time()
        async with asyncio.timeout(context.timeout_seconds) as deadline:
            built = await self.factory.build(
                agent_key=agent_key,
                environment=environment,
                context=context,
                prompt_variables=prompt_variables,
            )
            # Count assembly against the definition's deadline too.
            if asyncio.get_running_loop().time() >= started + built.timeout_seconds:
                raise TimeoutError("Agent assembly exceeded its execution deadline")
            deadline.reschedule(started + built.timeout_seconds)

            async with self._trace(agent_key, context) as trace_id:
                result = await Runner.run(
                    built.agent,
                    input=_run_input(built.prompt_messages, user_input, business_context),
                    context=context,
                    max_turns=built.max_turns,
                    run_config=RunConfig(tracing_disabled=self._tracer is None),
                )

        return AgentRunResult(
            output=result.final_output,
            agent_key=agent_key,
            agent_version=built.agent_version,
            prompt_version=built.prompt_version,
            trace_id=trace_id,
            toolset=list(built.config.tools),
        )

    @asynccontextmanager
    async def _trace(self, agent_key: str, context: RuntimeContext):
        """Open a Langfuse span when tracing is on; otherwise do nothing."""
        tracer = self._tracer
        if tracer is None:
            yield None
            return
        try:
            with tracer.start_as_current_observation(name=f"agent:{agent_key}") as observation:
                tracer.update_current_trace(
                    user_id=context.user_id,
                    session_id=context.correlation_id,
                    metadata={**context.trace_metadata, "tenant_id": context.tenant_id},
                    tags=[f"env:{context.environment}", f"agent:{agent_key}"],
                )
                yield getattr(observation, "trace_id", None)
        finally:
            # The SDK batches pending spans; never extend a cancelled run to flush.
            if not asyncio.current_task().cancelling():
                await asyncio.to_thread(tracer.flush)
