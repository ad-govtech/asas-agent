"""asas-agent: agents as configuration.

An agent is a versioned row in Postgres: a prompt reference, a model, the tools
it may use and the limits it runs under. This package resolves that definition
and runs it on the OpenAI Agents SDK, with file prompts or optional Langfuse.
"""

from asas_agent.bootstrap import Platform, build_platform, build_runtime
from asas_agent.config import Settings, get_settings
from asas_agent.migrate import migrate
from asas_agent.registry.capabilities import capability
from asas_agent.registry.capabilities import registry as capability_registry
from asas_agent.registry.outputs import output_schema
from asas_agent.registry.outputs import registry as output_registry
from asas_agent.registry.schema import AgentConfig, ModelRef, PromptRef
from asas_agent.runtime.context import CapabilityPolicy, RuntimeContext

__all__ = [
    "AgentConfig",
    "CapabilityPolicy",
    "ModelRef",
    "Platform",
    "PromptRef",
    "RuntimeContext",
    "Settings",
    "build_platform",
    "build_runtime",
    "capability",
    "capability_registry",
    "get_settings",
    "migrate",
    "output_registry",
    "output_schema",
]

__version__ = "0.1.0"
