"""Running an agent, and recording what ran.

A run is given two things: the `inputs` its prompt asks for, and optionally a
`message` from whoever is talking to it. Values always fill placeholders and a
message is always a message, so adding a placeholder to a prompt never changes
what the other one means.
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from asas_agent.integrations.prompts import PromptMessage
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.factory import AgentFactory

#: What a run may be called, in bytes, because that is what a backend stores.
RUN_NAME_MAX_BYTES = 200

#: How the runtime names a run of an agent. A request cannot claim one.
RUNTIME_NAME_PREFIX = "agent:"


class RunInputError(ValueError):
    """Raised when what the caller sent cannot start a run."""


def _run_name(name: str | None, agent_key: str) -> str:
    """What this run is called in the trace.

    A product that fans out runs one agent many times over - once per rubric
    area, once per candidate - and has to tell those runs apart afterwards,
    which is what an evaluation harness reads. Without a name, the agent's own.
    """
    if name is None:
        return f"{RUNTIME_NAME_PREFIX}{agent_key}"

    cleaned = " ".join(name.split())
    if not cleaned:
        raise RunInputError("A run name cannot be blank. Leave it out to use the agent's own name.")

    hidden = {c for c in cleaned if unicodedata.category(c) in {"Cc", "Cf"}}
    if hidden:
        raise RunInputError(
            "A run name cannot contain control or formatting characters: "
            f"{', '.join(f'U+{ord(c):04X}' for c in sorted(hidden))}."
        )

    size = len(cleaned.encode("utf-8"))
    if size > RUN_NAME_MAX_BYTES:
        raise RunInputError(f"A run name is at most {RUN_NAME_MAX_BYTES} bytes; this one is {size}.")

    if cleaned.startswith(RUNTIME_NAME_PREFIX):
        raise RunInputError(
            f"A run name cannot start with {RUNTIME_NAME_PREFIX!r}: that is how the runtime names a run of an "
            "agent, and a reader takes it at its word. Name this run after what it is doing."
        )
    return cleaned


def _run_input(prompt_messages: tuple[PromptMessage, ...], message: str) -> list[dict[str, str]]:
    """What the run starts from: the prompt's own user message, then the caller's.

    One of the two has to exist. A prompt that opens the conversation itself
    needs no message; an agent answering someone needs theirs.
    """
    items = [{"role": m.role, "content": m.content} for m in prompt_messages]
    if message:
        items.append({"role": "user", "content": message})
    if not items:
        raise RunInputError(
            "This agent's prompt asks nothing on its own, so the run needs a message to answer. "
            "Either send one, or give the prompt a user message."
        )
    return items


@dataclass
class AgentRunResult:
    output: Any
    agent_key: str
    agent_version: int
    prompt_version: int | None
    trace_id: str | None
    #: What this run is called in the trace, so a caller can find it again.
    run_name: str
    toolset: list[str]


class AgentRuntime:
    def __init__(
        self,
        factory: AgentFactory,
        *,
        tracer=None,
        max_turns_ceiling: int = 20,
        timeout_ceiling_seconds: float = 300,
        inputs_max_bytes: int = 256_000,
        dependencies: dict[str, Any] | None = None,
    ):
        self.factory = factory
        self._tracer = tracer
        self.max_turns_ceiling = max_turns_ceiling
        self.timeout_ceiling_seconds = timeout_ceiling_seconds
        self.inputs_max_bytes = inputs_max_bytes
        self.default_dependencies = dict(dependencies or {})

    async def run(
        self,
        *,
        agent_key: str,
        environment: str,
        inputs: dict[str, Any] | None = None,
        message: str = "",
        run_name: str | None = None,
        context: RuntimeContext,
    ) -> AgentRunResult:
        from agents import RunConfig, Runner

        if context.max_turns < 1 or context.timeout_seconds <= 0:
            raise ValueError("Runtime turn and timeout limits must be positive")
        self._check_size(inputs)
        name = _run_name(run_name, agent_key)
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
                inputs=inputs,
            )
            # Count assembly against the definition's deadline too.
            if asyncio.get_running_loop().time() >= started + built.timeout_seconds:
                raise TimeoutError("Agent assembly exceeded its execution deadline")
            deadline.reschedule(started + built.timeout_seconds)

            async with self._trace(name, agent_key, context) as trace_id:
                result = await Runner.run(
                    built.agent,
                    input=_run_input(built.prompt_messages, message),
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
            run_name=name,
            toolset=list(built.config.tools),
        )

    def _check_size(self, inputs: dict[str, Any] | None) -> None:
        """Inputs land in the instructions, which are re-sent on every turn of a run."""
        if not inputs:
            return
        size = len(json.dumps(inputs, ensure_ascii=False, default=str).encode("utf-8"))
        if size > self.inputs_max_bytes:
            raise RunInputError(
                f"The inputs are {size} bytes, over the {self.inputs_max_bytes} byte limit. "
                "Send less, or raise ASAS_INPUTS_MAX_BYTES."
            )

    @asynccontextmanager
    async def _trace(self, name: str, agent_key: str, context: RuntimeContext):
        """Open a Langfuse span when tracing is on; otherwise do nothing."""
        tracer = self._tracer
        if tracer is None:
            yield None
            return
        try:
            with tracer.start_as_current_observation(name=name) as observation:
                tracer.update_current_trace(
                    name=name,
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
