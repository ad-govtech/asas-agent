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

#: What a trace name may be. Long enough for a prompt name and a discriminator.
TRACE_NAME_MAX_LENGTH = 200

#: Facts the runtime records itself. A caller cannot overwrite them in a trace.
RESERVED_TRACE_KEYS = frozenset(
    {
        "agent_key",
        "agent_version",
        "environment",
        "prompt_name",
        "prompt_version",
        "prompt_variables",
        "instructions_digest",
        "model_provider",
        "model_name",
        "toolset",
        "tenant_id",
    }
)


class RunInputError(ValueError):
    """Raised when what the caller sent cannot start a run."""


def _trace_name(name: str | None, agent_key: str) -> str:
    """What this run is called in the trace.

    A product that fans out runs one agent many times over - once per rubric
    area, once per candidate - and needs to tell those runs apart afterwards,
    which is what an evaluation harness reads. Without a name, the agent's own.
    """
    if name is None:
        return f"agent:{agent_key}"
    cleaned = " ".join(name.split())
    if not cleaned:
        raise RunInputError("A trace name cannot be blank. Leave it out to use the agent's own name.")
    if len(cleaned) > TRACE_NAME_MAX_LENGTH:
        raise RunInputError(f"A trace name is at most {TRACE_NAME_MAX_LENGTH} characters; this one is {len(cleaned)}.")
    return cleaned


def _trace_extras(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """A caller's own trace fields, which may add to what the runtime records but never replace it."""
    extras = metadata or {}
    reserved = sorted(RESERVED_TRACE_KEYS & set(extras))
    if reserved:
        raise RunInputError(
            f"The runtime records {', '.join(reserved)} itself, so a request cannot set them. "
            "Use different names for your own trace fields."
        )
    return dict(extras)


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
        if not user_input and not business_context:
            raise RunInputError(
                "This agent's prompt sends no messages of its own, "
                "so the run needs an input or a context to start from."
            )
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
    #: What this run is called in the trace, so a caller can find it again.
    trace_name: str
    toolset: list[str]


class AgentRuntime:
    def __init__(
        self,
        factory: AgentFactory,
        *,
        tracer=None,
        max_turns_ceiling: int = 20,
        timeout_ceiling_seconds: float = 300,
        prompt_variables_max_bytes: int = 256_000,
        dependencies: dict[str, Any] | None = None,
    ):
        self.factory = factory
        self._tracer = tracer
        self.max_turns_ceiling = max_turns_ceiling
        self.timeout_ceiling_seconds = timeout_ceiling_seconds
        self.prompt_variables_max_bytes = prompt_variables_max_bytes
        self.default_dependencies = dict(dependencies or {})

    async def run(
        self,
        *,
        agent_key: str,
        environment: str,
        user_input: str = "",
        business_context: dict[str, Any] | None = None,
        prompt_variables: dict[str, Any] | None = None,
        trace_name: str | None = None,
        trace_metadata: dict[str, Any] | None = None,
        context: RuntimeContext,
    ) -> AgentRunResult:
        from agents import RunConfig, Runner

        if context.max_turns < 1 or context.timeout_seconds <= 0:
            raise ValueError("Runtime turn and timeout limits must be positive")
        self._check_size(prompt_variables)
        name = _trace_name(trace_name, agent_key)
        context = replace(
            context,
            environment=environment,
            max_turns=min(context.max_turns, self.max_turns_ceiling),
            timeout_seconds=min(context.timeout_seconds, self.timeout_ceiling_seconds),
            dependencies={**self.default_dependencies, **context.dependencies},
            # A caller's fields first: what the factory records about the run
            # is written over them, and cannot be forged.
            trace_metadata={**_trace_extras(trace_metadata), **context.trace_metadata},
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

            async with self._trace(name, agent_key, context) as trace_id:
                result = await Runner.run(
                    built.agent,
                    input=_run_input(built.prompt_messages, user_input, business_context),
                    context=context,
                    max_turns=built.max_turns,
                    run_config=RunConfig(
                        tracing_disabled=self._tracer is None,
                        workflow_name=name,
                        group_id=context.correlation_id,
                        trace_metadata=dict(context.trace_metadata),
                    ),
                )

        return AgentRunResult(
            output=result.final_output,
            agent_key=agent_key,
            agent_version=built.agent_version,
            prompt_version=built.prompt_version,
            trace_id=trace_id,
            trace_name=name,
            toolset=list(built.config.tools),
        )

    def _check_size(self, prompt_variables: dict[str, Any] | None) -> None:
        """Prompt variables land in the instructions, which are re-sent on every turn."""
        if not prompt_variables:
            return
        size = len(json.dumps(prompt_variables, ensure_ascii=False, default=str).encode("utf-8"))
        if size > self.prompt_variables_max_bytes:
            raise RunInputError(
                f"The prompt variables are {size} bytes, over the {self.prompt_variables_max_bytes} byte limit. "
                "Send large data as context, or raise ASAS_PROMPT_VARIABLES_MAX_BYTES."
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
