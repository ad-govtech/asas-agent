"""Agent factory: configuration in, an Agents SDK agent out.

Nothing here is specific to one product. The factory resolves the prompt, the
model, the approved tools, the sub-agents and the output schema, and records
what it resolved so the trace can be reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from asas_agent.integrations.models import ModelError, ModelRegistry
from asas_agent.integrations.prompts import PromptProvider
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
        visited: set[str] | None = None,
    ) -> BuiltAgent:
        from agents import Agent, ModelSettings

        visited = visited or set()
        node = f"{agent_key}:{environment}"
        if node in visited:
            raise CyclicAgentError(f"Sub-agents form a loop at {node}")
        visited.add(node)

        definition = await self.repository.get_active(agent_key=agent_key, environment=environment)
        config = definition.config

        resolved_prompt = await self.prompts.resolve(config.prompt)
        model = self.models.resolve(config.model.provider, config.model.name)
        capability = self.models.capability(config.model.provider, config.model.name)

        if config.tools and not capability.tool_calling:
            raise ModelError(f"{config.model.provider}:{config.model.name} cannot call tools, but tools are configured")
        if config.output.schema_key and not capability.structured_output:
            raise ModelError(
                f"{config.model.provider}:{config.model.name} cannot return structured output, "
                f"but the schema {config.output.schema_key!r} is configured"
            )

        tools = self.capabilities.resolve_many(config.tools, context)
        handoffs = []

        for ref in config.sub_agents:
            built = await self.build(
                agent_key=ref.agent_key,
                environment=ref.environment,
                context=context,
                visited=set(visited),
            )
            if ref.mode == "tool":
                tools.append(
                    built.agent.as_tool(
                        tool_name=ref.tool_name or ref.agent_key.replace("-", "_"),
                        tool_description=ref.description or f"Ask the {ref.agent_key} specialist.",
                    )
                )
            else:
                handoffs.append(built.agent)

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
            }
        )

        agent = Agent(
            name=config.name,
            instructions=resolved_prompt.text,
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
        )
