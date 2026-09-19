"""Validate a release without building tools, executing actions, or calling models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from asas_agent.integrations.models import ModelError, ModelRegistry

from .capabilities import CapabilityRegistry
from .guardrails import GuardrailRegistry
from .outputs import OutputSchemaRegistry
from .schema import AgentConfig


class DefinitionError(ValueError):
    """A release references an invalid or cyclic agent graph."""


class DefinitionValidator:
    def __init__(
        self,
        models: ModelRegistry,
        capabilities: CapabilityRegistry,
        outputs: OutputSchemaRegistry,
        guardrails: GuardrailRegistry,
    ):
        self.models = models
        self.capabilities = capabilities
        self.outputs = outputs
        self.guardrails = guardrails

    async def validate(
        self,
        config: AgentConfig,
        *,
        agent_key: str,
        resolve: Callable[[str, str], Awaitable[AgentConfig]],
        visited: frozenset[str] = frozenset(),
    ) -> None:
        if agent_key in visited:
            raise DefinitionError(f"Sub-agents form a loop at {agent_key}")
        visited = visited | {agent_key}
        capabilities = self.models.capability(config.model.provider, config.model.name)
        if (config.tools or config.sub_agents) and not capabilities.tool_calling:
            raise ModelError(f"{config.model.provider}:{config.model.name} cannot call tools or delegate")
        if config.output.schema_key and not capabilities.structured_output:
            raise ModelError(f"{config.model.provider}:{config.model.name} cannot return structured output")
        self.models.validated_settings(config.model.settings)
        for key in config.tools:
            self.capabilities.get(key)  # Validate the name, never invoke the factory.
        self.outputs.resolve(config.output.schema_key)
        self.guardrails.resolve_many(config.guardrails)
        for ref in config.sub_agents:
            if ref.agent_key in visited:
                raise DefinitionError(f"Sub-agents form a loop at {ref.agent_key}")
            child = await resolve(ref.agent_key, ref.environment)
            await self.validate(child, agent_key=ref.agent_key, resolve=resolve, visited=visited)
