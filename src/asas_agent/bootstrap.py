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
from asas_agent.registry.lookups import SharedAgentLookups
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

    if not _instrument_spans():
        # Whatever the reason - the package is missing, or it refused to attach
        # to this version of the SDK - the Agents SDK is left with its default
        # processor, which posts traces to OpenAI. Choosing an internal trace
        # backend must never turn on an external one, so clear it.
        from agents import set_trace_processors

        set_trace_processors([])

    return client


def _instrument_spans() -> bool:
    """Route the SDK's own spans to the configured backend. True if that worked.

    `instrument()` reports a version mismatch by logging and returning, so the
    only trustworthy check is whether a processor was actually installed.
    """
    try:
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    except ImportError:
        return False  # Install the `tracing` extra for span-level detail.

    try:
        OpenAIAgentsInstrumentor().instrument(exclusive_processor=True)
    except Exception:  # noqa: BLE001 - any failure here means "not instrumented"
        return False

    from agents.tracing import get_trace_provider

    installed = getattr(get_trace_provider(), "_multi_processor", None)
    processors = getattr(installed, "_processors", ())
    return any(type(processor).__module__.startswith("openinference") for processor in processors)


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
    # A fan-out asks the same question many times in the same moment.
    repository: Any = SharedAgentLookups(
        AgentRepository(create_session_factory(engine), prompts=prompts, models=models)
    )

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
        prompt_variables_max_bytes=settings.prompt_variables_max_bytes,
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
