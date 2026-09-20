"""Choosing an internal trace backend must never leave an external one installed.

The Agents SDK ships a processor that posts traces to OpenAI. Enabling Langfuse
replaces it only if the OpenInference instrumentation actually attaches, and
`instrument()` reports a version mismatch by logging and returning rather than
raising. So the runtime checks, rather than assuming.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agents.tracing import get_trace_provider

from asas_agent.bootstrap import _build_tracer, _instrument_spans
from asas_agent.config import Settings


@pytest.fixture
def settings(monkeypatch):
    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    return Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test", OPENAI_API_KEY="test")


def processors():
    return get_trace_provider()._multi_processor._processors


@pytest.fixture(autouse=True)
def _restore_processors():
    before = processors()
    yield
    from agents import set_trace_processors

    set_trace_processors(list(before))


def _instrumentor(monkeypatch, instrument):
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.openai_agents",
        SimpleNamespace(OpenAIAgentsInstrumentor=lambda: SimpleNamespace(instrument=instrument)),
    )


def test_an_instrumentor_that_did_not_attach_is_not_believed(settings, monkeypatch):
    """A version mismatch is reported in a log line, not an exception."""
    _instrumentor(monkeypatch, lambda **kwargs: None)
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    assert _instrument_spans() is False
    _build_tracer(settings)
    assert processors() == (), "the SDK's own exporter was left pointing at OpenAI"


def test_an_instrumentor_that_raises_is_not_believed_either(settings, monkeypatch):
    def refuse(**kwargs):
        raise RuntimeError("incompatible")

    _instrumentor(monkeypatch, refuse)
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    assert _instrument_spans() is False
    _build_tracer(settings)
    assert processors() == ()


def test_a_missing_package_leaves_nothing_exporting(settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.openai_agents", None)
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    assert _instrument_spans() is False
    _build_tracer(settings)
    assert processors() == ()


def test_an_instrumentor_that_did_attach_is_left_in_place(settings, monkeypatch):
    processor_type = type("OpenInferenceTracingProcessor", (), {})
    processor_type.__module__ = "openinference.instrumentation.openai_agents._processor"
    attached = processor_type()

    def attach(**kwargs):
        from agents import set_trace_processors

        set_trace_processors([attached])

    _instrumentor(monkeypatch, attach)
    assert _instrument_spans() is True
    assert processors() == (attached,)


def test_tracing_switched_off_touches_nothing(settings):
    settings.tracing_provider = "none"
    assert _build_tracer(settings) is None


def openai_exporting_processor():
    """What the SDK installs by default: a batch processor posting to OpenAI."""
    from agents.tracing.processors import BackendSpanExporter, BatchTraceProcessor

    processor = object.__new__(BatchTraceProcessor)
    processor._exporter = object.__new__(BackendSpanExporter)
    return processor


def internal_processor():
    processor_type = type("OpenInferenceTracingProcessor", (), {})
    processor_type.__module__ = "openinference.instrumentation.openai_agents._processor"
    return processor_type()


def test_a_process_already_instrumented_without_exclusivity_still_loses_openais_exporter(settings, monkeypatch):
    """`instrument(exclusive_processor=True)` logs and returns when it has already run, changing nothing."""
    from agents import set_trace_processors

    internal = internal_processor()
    set_trace_processors([openai_exporting_processor(), internal])

    # Attaching again is a no-op, exactly as the real instrumentor behaves here.
    _instrumentor(monkeypatch, lambda **kwargs: None)
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    _build_tracer(settings)

    assert processors() == (internal,), "presence of an internal processor was taken for exclusivity"


def test_another_librarys_processor_is_left_alone(settings, monkeypatch):
    """What someone else installed is their business; what reaches OpenAI is ours."""
    from agents import set_trace_processors

    theirs = SimpleNamespace(on_trace_start=lambda *a: None)
    set_trace_processors([theirs, openai_exporting_processor()])

    _instrumentor(monkeypatch, lambda **kwargs: None)
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    _build_tracer(settings)

    assert processors() == (theirs,)


def test_a_list_with_nothing_exporting_to_openai_is_not_touched(settings, monkeypatch):
    from agents import set_trace_processors

    internal = internal_processor()
    set_trace_processors([internal])

    _instrumentor(monkeypatch, lambda **kwargs: None)
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    _build_tracer(settings)

    assert processors() == (internal,)
