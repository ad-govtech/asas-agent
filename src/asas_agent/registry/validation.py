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


def _name(node: tuple[str, str | None]) -> str:
    agent_key, environment = node
    return f"{agent_key}:{environment}" if environment else agent_key


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
        environment: str | None = None,
        resolve: Callable[[str, str], Awaitable[AgentConfig]],
        visited: frozenset[tuple[str, str | None]] = frozenset(),
    ) -> None:
        """Check a release without building tools, calling models or running anything.

        A node in this graph is an agent *in an environment*, because that is
        what a sub-agent reference names and what the runtime resolves. One
        agent appearing twice in a path is only a loop when it is the same
        binding twice: `reviewer:production` may delegate to `advisor:staging`
        while production's advisor delegates to that reviewer, and nothing
        repeats. A definition being published has no environment yet, so it is
        its own node until a binding gives it one.
        """
        node = (agent_key, environment)
        if node in visited:
            raise DefinitionError(f"Sub-agents form a loop at {_name(node)}")
        visited = visited | {node}
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
            child_node = (ref.agent_key, ref.environment)
            if environment is None and ref.agent_key == agent_key:
                # A definition being published has no environment yet, so a
                # reference to itself cannot be told apart from the binding it
                # is about to become.
                raise DefinitionError(f"Sub-agents form a loop at {_name(child_node)}")
            if child_node in visited:
                raise DefinitionError(f"Sub-agents form a loop at {_name(child_node)}")
            child = await resolve(ref.agent_key, ref.environment)
            await self.validate(
                child,
                agent_key=ref.agent_key,
                environment=ref.environment,
                resolve=resolve,
                visited=visited,
            )
