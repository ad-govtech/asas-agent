"""Check a release before it is published, without running any part of it.

Every name in a definition is resolved here - the tools, the output schema, the
model and its settings - so a definition that could not run is refused while it
is still a draft, rather than at the first request that needs it.
"""

from __future__ import annotations

from asas_agent.integrations.models import ModelRegistry

from .capabilities import CapabilityRegistry
from .outputs import OutputSchemaRegistry
from .schema import AgentConfig


class DefinitionError(ValueError):
    """A release references something this runtime cannot resolve."""


class DefinitionValidator:
    def __init__(
        self,
        models: ModelRegistry,
        capabilities: CapabilityRegistry,
        outputs: OutputSchemaRegistry,
    ):
        self.models = models
        self.capabilities = capabilities
        self.outputs = outputs

    async def validate(self, config: AgentConfig, *, agent_key: str) -> None:
        self.models.assert_known(config.model.provider)
        self.models.resolve_settings(config.model.settings)
        for key in config.tools:
            self.capabilities.get(key)  # Validate the name, never invoke the factory.
        self.outputs.resolve(config.output.schema_key)
