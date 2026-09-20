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
    _instrument_spans()
    # Whether or not the instrumentation attached, and whatever else this
    # process has already installed, nothing may still be posting traces to
    # OpenAI: choosing an internal backend must never turn on an external one.
    _stop_exporting_to_openai()
    return client


def _instrument_spans() -> bool:
    """Route the SDK's own spans to the configured backend. True if that worked.

    `instrument()` reports both a version mismatch and an already-instrumented
    process by logging and returning, so the only trustworthy check is whether
    a processor of its own is installed afterwards.
    """
    try:
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
    except ImportError:
        return False  # Install the `tracing` extra for span-level detail.

    try:
        OpenAIAgentsInstrumentor().instrument(exclusive_processor=True)
    except Exception:  # noqa: BLE001 - any failure here means "not instrumented"
        return False

    return any(_is_internal(processor) for processor in _processors())


def _stop_exporting_to_openai() -> None:
    """Remove any processor posting to OpenAI's trace backend, and leave the rest alone.

    `instrument(exclusive_processor=True)` does not make the list exclusive in
    a process that was already instrumented without it: it logs and returns,
    leaving the SDK's own exporter beside the one that was added. What another
    library installed is its business; what reaches OpenAI is ours.
    """
    keep = [processor for processor in _processors() if not _exports_to_openai(processor)]
    if len(keep) != len(_processors()):
        from agents import set_trace_processors

        set_trace_processors(keep)


def _processors() -> tuple[Any, ...]:
    from agents.tracing import get_trace_provider

    multi = getattr(get_trace_provider(), "_multi_processor", None)
    return tuple(getattr(multi, "_processors", ()))


def _is_internal(processor: Any) -> bool:
    return type(processor).__module__.startswith("openinference")


def _exports_to_openai(processor: Any) -> bool:
    from agents.tracing.processors import BackendSpanExporter

    return isinstance(getattr(processor, "_exporter", None), BackendSpanExporter)


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
    )

    runtime = AgentRuntime(
        factory,
        tracer=tracer,
        max_turns_ceiling=settings.max_turns_ceiling,
        timeout_ceiling_seconds=settings.timeout_ceiling_seconds,
        inputs_max_bytes=settings.inputs_max_bytes,
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
