"""Wiring. One call builds the runtime an application needs.

    from asas_agent import build_runtime
    runtime = build_runtime()

The app registers its own capabilities and output schemas before calling this.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any

from asas_agent.config import Settings, get_settings
from asas_agent.integrations.models import ModelRegistry
from asas_agent.integrations.prompts import PromptError, PromptProvider, build_prompt_provider, langfuse_client
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
    prompts: PromptProvider | None
    models: ModelRegistry
    runtime: AgentRuntime

    async def close(self) -> None:
        try:
            await self.models.close()
        finally:
            await self.engine.dispose()


def _build_tracer(settings: Settings):
    if not settings.tracing_enabled or settings.tracing_provider == "none":
        return None

    client = langfuse_client(settings)

    try:  # Traces every model and tool call the Agents SDK makes.
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

        OpenAIAgentsInstrumentor().instrument(exclusive_processor=True)
    except ImportError:
        # Without span instrumentation, keep only our Langfuse run observation.
        # Selecting an internal trace backend must not enable OpenAI's exporter.
        from agents import set_trace_processors

        set_trace_processors([])

    return client


def build_platform(
    settings: Settings | None = None,
    *,
    dependencies: dict[str, Any] | None = None,
    require_prompts: bool = True,
) -> Platform:
    settings = settings or get_settings()
    for module in settings.registration_modules.split(","):
        if module.strip():
            import_module(module.strip())

    try:
        prompts = build_prompt_provider(settings)
    except PromptError:
        if require_prompts:
            raise
        prompts = None  # `doctor` reports the problem instead of failing to start

    tracer = _build_tracer(settings)
    engine = create_engine(settings.database_url)
    models = ModelRegistry(settings=settings)
    repository = AgentRepository(create_session_factory(engine), prompts=prompts, models=models)

    factory = AgentFactory(
        repository=repository,
        prompts=prompts,
        models=models,
        capabilities=capability_module.registry,
        outputs=output_module.registry,
        guardrails=guardrail_module.registry,
    )

    runtime = AgentRuntime(
        factory,
        tracer=tracer,
        max_turns_ceiling=settings.max_turns_ceiling,
        timeout_ceiling_seconds=settings.timeout_ceiling_seconds,
        dependencies=dependencies,
    )

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
