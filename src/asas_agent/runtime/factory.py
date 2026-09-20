"""Agent factory: configuration in, an Agents SDK agent out.

Nothing here is specific to one product. The factory resolves the prompt, the
model, the approved tools, the sub-agents and the output schema, and records
what it resolved so the trace can be reproduced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from asas_agent.integrations.models import ModelError, ModelRegistry
from asas_agent.integrations.prompts import PromptError, PromptMessage, PromptProvider
from asas_agent.registry.capabilities import CapabilityRegistry
from asas_agent.registry.guardrails import GuardrailRegistry
from asas_agent.registry.outputs import OutputSchemaRegistry
from asas_agent.registry.repository import AgentRepository
from asas_agent.registry.schema import AgentConfig
from asas_agent.runtime.context import RuntimeContext


class CyclicAgentError(RuntimeError):
    """Raised when sub-agents reference each other in a loop."""


@dataclass
class BuiltAgent:
    agent: Any
    agent_version: int
    prompt_version: int | None
    config: AgentConfig
    max_turns: int
    timeout_seconds: float
    #: The user and assistant messages of a chat prompt, which open the run.
    prompt_messages: tuple[PromptMessage, ...] = ()


def _bounded_tool(tool, timeout_seconds: float):
    invoke = tool.on_invoke_tool

    async def bounded(context, arguments):
        async with asyncio.timeout(timeout_seconds):
            return await invoke(context, arguments)

    tool.on_invoke_tool = bounded
    return tool


class AgentFactory:
    def __init__(
        self,
        *,
        repository: AgentRepository,
        prompts: PromptProvider,
        models: ModelRegistry,
        capabilities: CapabilityRegistry,
        outputs: OutputSchemaRegistry,
        guardrails: GuardrailRegistry,
    ):
        self.repository = repository
        self.prompts = prompts
        self.models = models
        self.capabilities = capabilities
        self.outputs = outputs
        self.guardrails = guardrails

    async def build(
        self,
        *,
        agent_key: str,
        environment: str,
        context: RuntimeContext,
        prompt_variables: dict[str, Any] | None = None,
        is_sub_agent: bool = False,
        visited: set[str] | None = None,
    ) -> BuiltAgent:
        from agents import Agent, ModelSettings

        visited = visited or set()
        node = f"{agent_key}:{environment}"
        if node in visited:
            raise CyclicAgentError(f"Sub-agents form a loop at {node}")
        visited.add(node)

        started = asyncio.get_running_loop().time()
        definition = await self.repository.get_active(agent_key=agent_key, environment=environment)
        config = definition.config

        async with asyncio.timeout_at(started + min(context.timeout_seconds, config.runtime.timeout_seconds)):
            # A sub-agent keeps whatever its own definition sets: the caller
            # addressed the parent and cannot know a specialist's variables.
            values = prompt_variables
            if is_sub_agent and values:
                values = {k: v for k, v in values.items() if k not in config.prompt.variables}
            resolved_prompt = await self.prompts.resolve(config.prompt, values)
            if is_sub_agent and resolved_prompt.messages:
                # A sub-agent is handed the caller's or the parent's input, so
                # there is nowhere to put its own opening messages.
                raise PromptError(
                    f"Sub-agent {agent_key!r} uses a chat prompt with user or assistant messages, "
                    "which a sub-agent cannot send. Move that content into its system message."
                )
            model = self.models.resolve(config.model.provider, config.model.name)
            capability = self.models.capability(config.model.provider, config.model.name)

            if (config.tools or config.sub_agents) and not capability.tool_calling:
                raise ModelError(
                    f"{config.model.provider}:{config.model.name} cannot call tools, but tools are configured"
                )
            if config.output.schema_key and not capability.structured_output:
                raise ModelError(
                    f"{config.model.provider}:{config.model.name} cannot return structured output, "
                    f"but the schema {config.output.schema_key!r} is configured"
                )

            tools = self.capabilities.resolve_many(config.tools, context)
            handoffs = []
            max_turns = min(context.max_turns, config.runtime.max_turns)
            timeout_seconds = min(context.timeout_seconds, config.runtime.timeout_seconds)

            for ref in config.sub_agents:
                built = await self.build(
                    agent_key=ref.agent_key,
                    environment=ref.environment,
                    context=context,
                    prompt_variables=prompt_variables,
                    is_sub_agent=True,
                    visited=set(visited),
                )
                if ref.mode == "tool":
                    tools.append(
                        _bounded_tool(
                            built.agent.as_tool(
                                tool_name=ref.tool_name or ref.agent_key.replace("-", "_"),
                                tool_description=ref.description or f"Ask the {ref.agent_key} specialist.",
                                max_turns=built.max_turns,
                                failure_error_function=None,
                            ),
                            built.timeout_seconds,
                        )
                    )
                else:
                    handoffs.append(built.agent)
                    # Handoffs share one SDK run, so use the strictest chain budget.
                    max_turns = min(max_turns, built.max_turns)
                    timeout_seconds = min(timeout_seconds, built.timeout_seconds)

            guardrails = self.guardrails.resolve_many(config.guardrails)

            context.trace_metadata.update(
                {
                    "agent_key": agent_key,
                    "agent_version": definition.version,
                    "environment": environment,
                    "prompt_name": resolved_prompt.name,
                    "prompt_version": resolved_prompt.version,
                    "model_provider": config.model.provider,
                    "model_name": config.model.name,
                    "toolset": list(config.tools),
                    # A request's values now help decide the instructions, so a
                    # trace records which names were filled and a digest of the
                    # text that resulted. The values themselves are the
                    # caller's data and are not copied here.
                    "prompt_variables": list(resolved_prompt.filled),
                    "instructions_digest": sha256(resolved_prompt.instructions.encode("utf-8")).hexdigest()[:12],
                }
            )

            agent = Agent(
                name=config.name,
                instructions=resolved_prompt.instructions,
                model=model,
                model_settings=ModelSettings(**self.models.validated_settings(config.model.settings)),
                tools=tools,
                handoffs=handoffs,
                output_type=self.outputs.resolve(config.output.schema_key),
                input_guardrails=guardrails.input,
                output_guardrails=guardrails.output,
            )

            return BuiltAgent(
                agent=agent,
                agent_version=definition.version,
                prompt_version=resolved_prompt.version,
                config=config,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
                prompt_messages=resolved_prompt.messages,
            )
