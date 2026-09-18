"""Wiring. One call builds the runtime an application needs.

    from asas_agent import build_runtime
    runtime = build_runtime()

The app registers its own capabilities and output schemas before calling this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from asas_agent.config import Settings, get_settings
from asas_agent.integrations.models import ModelRegistry
from asas_agent.integrations.prompts import PromptProvider, build_prompt_provider
from asas_agent.registry import capabilities as capability_module
from asas_agent.registry import guardrails as guardrail_module
from asas_agent.registry import outputs as output_module
from asas_agent.registry.db import create_engine, create_session_factory
from asas_agent.registry.repository import AgentRepository
from asas_agent.runtime.factory import AgentFactory
from asas_agent.runtime.runner import AgentRuntime


@dataclass
class Platform:
    """Everything an app or the CLI needs, built once per process."""

    settings: Settings
    engine: Any
    repository: AgentRepository
    prompts: PromptProvider
    models: ModelRegistry
    runtime: AgentRuntime

    async def close(self) -> None:
        await self.engine.dispose()


def _build_tracer(settings: Settings):
    if not (settings.tracing_enabled and settings.langfuse_configured):
        return None

    from langfuse import get_client

    client = get_client()

    try:  # Traces every model and tool call the Agents SDK makes.
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

        OpenAIAgentsInstrumentor().instrument()
    except ImportError:
        pass  # Install the `tracing` extra for span-level detail.

    return client


def build_platform(
    settings: Settings | None = None,
    *,
    dependencies: dict[str, Any] | None = None,
    require_prompts: bool = True,
) -> Platform:
    settings = settings or get_settings()

    engine = create_engine(settings.database_url)
    repository = AgentRepository(create_session_factory(engine))

    try:
        prompts = build_prompt_provider(settings)
    except Exception:
        if require_prompts:
            raise
        prompts = None  # `doctor` reports the problem instead of failing to start

    models = ModelRegistry(settings=settings)

    factory = AgentFactory(
        repository=repository,
        prompts=prompts,
        models=models,
        capabilities=capability_module.registry,
        outputs=output_module.registry,
        guardrails=guardrail_module.registry,
    )

    runtime = AgentRuntime(factory, tracer=_build_tracer(settings))

    if dependencies:
        runtime.default_dependencies = dependencies  # type: ignore[attr-defined]

    return Platform(
        settings=settings,
        engine=engine,
        repository=repository,
        prompts=prompts,
        models=models,
        runtime=runtime,
    )


def build_runtime(settings: Settings | None = None) -> AgentRuntime:
    return build_platform(settings).runtime
