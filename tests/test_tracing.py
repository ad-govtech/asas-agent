"""What a run is called, and what is recorded about it.

One agent is often run many times over in a second - once per rubric area, once
per candidate - and afterwards someone has to tell those runs apart: a person
reading traces, or an evaluation harness that groups generations by name.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents import Runner

from asas_agent.bootstrap import build_platform
from asas_agent.config import Settings
from asas_agent.registry.repository import AgentDefinition
from asas_agent.registry.schema import AgentConfig
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.runner import TRACE_NAME_MAX_BYTES, RunInputError


def _like_langfuse(method: str, kwargs: dict) -> None:
    """Fail the way the real client would if we called it with arguments it does not take."""
    langfuse = pytest.importorskip("langfuse")
    inspect.signature(getattr(langfuse.Langfuse, method)).bind(None, **kwargs)


class Tracer:
    """A stand-in Langfuse client that records what it was told, and only accepts what the real one does."""

    def __init__(self):
        self.observations: list[str] = []
        self.traces: list[dict] = []
        self.flushes = 0

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        _like_langfuse("start_as_current_observation", kwargs)
        self.observations.append(kwargs.get("name"))
        yield SimpleNamespace(trace_id="trace-1")

    def update_current_trace(self, **kwargs):
        _like_langfuse("update_current_trace", kwargs)
        self.traces.append(kwargs)

    def flush(self):
        self.flushes += 1


@pytest.fixture
def settings(monkeypatch):
    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    return Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test", OPENAI_API_KEY="test")


@pytest.fixture
def platform(settings, monkeypatch):
    built = build_platform(settings)
    config = AgentConfig.model_validate(
        {
            "name": "Scorer",
            "prompt": {"name": "scorer", "snapshot": "You score {{area}}.", "variables": {"area": "Delivery"}},
            "model": {"provider": "openai", "name": "gpt-5-nano"},
        }
    )
    definition = AgentDefinition(
        agent_key="scorer",
        version=4,
        status="published",
        config=config,
        created_at=datetime.now(UTC),
        created_by="test",
        published_at=datetime.now(UTC),
    )
    monkeypatch.setattr(built.repository._repository, "get_active", AsyncMock(return_value=definition))
    monkeypatch.setattr(Runner, "run", AsyncMock(return_value=SimpleNamespace(final_output="done")))
    return built


def context(**overrides):
    return RuntimeContext(tenant_id="DGE", user_id="U1", correlation_id="REQ-1", **overrides)


async def run(platform, **kwargs):
    return await platform.runtime.run(
        agent_key="scorer", environment="dev", user_input="hi", context=context(), **kwargs
    )


async def test_a_run_is_named_after_its_agent_by_default(platform):
    tracer = Tracer()
    platform.runtime._tracer = tracer
    try:
        result = await run(platform)
    finally:
        await platform.close()

    assert result.trace_name == "agent:scorer"
    assert tracer.observations == ["agent:scorer"]
    assert tracer.traces[0]["name"] == "agent:scorer"


async def test_a_run_can_be_named_for_this_call(platform):
    """`dimensions-summary-generator-Delivery` is one fan-out branch, not just "the agent"."""
    tracer = Tracer()
    platform.runtime._tracer = tracer
    try:
        result = await run(platform, trace_name="dimensions-summary-generator-Delivery")
    finally:
        await platform.close()

    assert result.trace_name == "dimensions-summary-generator-Delivery"
    assert tracer.observations == ["dimensions-summary-generator-Delivery"]
    assert tracer.traces[0]["name"] == "dimensions-summary-generator-Delivery"
    # The agent is still a tag, so every branch is findable as one agent.
    assert "agent:scorer" in tracer.traces[0]["tags"]


async def test_a_request_is_not_carried_into_the_sdks_own_trace(platform):
    """The SDK's trace may be exported elsewhere, so a caller's name and fields stay out of it."""
    tracer = Tracer()
    platform.runtime._tracer = tracer
    try:
        await run(platform, trace_name="score-summarizer", trace_metadata={"candidate_id": "C-17"})
    finally:
        await platform.close()

    run_config = Runner.run.call_args.kwargs["run_config"]
    assert run_config.workflow_name == "Agent workflow"  # The SDK's own default.
    assert run_config.group_id is None
    assert run_config.trace_metadata is None
    # It all went to the observation this run opened instead.
    assert tracer.observations == ["score-summarizer"]
    assert tracer.traces[0]["metadata"]["candidate_id"] == "C-17"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("  score summarizer  ", "score summarizer"),
        ("score\nsummarizer", "score summarizer"),
    ],
)
async def test_a_name_is_tidied_before_it_is_recorded(platform, name, expected):
    try:
        assert (await run(platform, trace_name=name)).trace_name == expected
    finally:
        await platform.close()


@pytest.mark.parametrize("name", ["", "   ", "x" * (TRACE_NAME_MAX_BYTES + 1)])
async def test_a_name_that_cannot_be_used_is_refused(platform, name):
    try:
        with pytest.raises(RunInputError):
            await run(platform, trace_name=name)
    finally:
        await platform.close()


async def test_a_caller_can_record_its_own_fields(platform):
    """The engine tags a run with the area or candidate it is about."""
    tracer = Tracer()
    platform.runtime._tracer = tracer
    try:
        await run(platform, trace_metadata={"area": "Delivery", "candidate_id": "C-17"})
    finally:
        await platform.close()

    metadata = tracer.traces[0]["metadata"]
    assert metadata["area"] == "Delivery"
    assert metadata["candidate_id"] == "C-17"
    # Alongside, not instead of, what the runtime knows about the run.
    assert metadata["agent_version"] == 4
    assert metadata["prompt_name"] == "scorer"
    assert metadata["tenant_id"] == "DGE"


@pytest.mark.parametrize("key", ["agent_version", "prompt_version", "toolset", "tenant_id"])
async def test_a_caller_cannot_rewrite_what_the_runtime_records(platform, key):
    """A trace is evidence of what ran; a request must not be able to forge it."""
    try:
        with pytest.raises(RunInputError, match=key):
            await run(platform, trace_metadata={key: "forged"})
    finally:
        await platform.close()


async def test_the_caller_context_is_not_changed_by_a_run(platform):
    """A context is often reused across a fan-out; a run must not leave its fields behind."""
    shared = context()
    try:
        await platform.runtime.run(
            agent_key="scorer",
            environment="dev",
            user_input="hi",
            trace_metadata={"area": "Delivery"},
            context=shared,
        )
    finally:
        await platform.close()

    assert shared.trace_metadata == {}


async def test_a_run_without_tracing_still_reports_its_name(platform):
    try:
        result = await run(platform, trace_name="score-summarizer")
    finally:
        await platform.close()

    assert result.trace_id is None  # Tracing is off by default.
    assert result.trace_name == "score-summarizer"
    assert Runner.run.call_args.kwargs["run_config"].tracing_disabled is True


async def test_the_api_carries_the_name_in_and_back_out(platform):
    import httpx

    from asas_agent.api.app import create_app

    body = {
        "agent_key": "scorer",
        "environment": "dev",
        "input": "hi",
        "trace_name": "candidate-job-scorer-Delivery",
        "trace_metadata": {"area": "Delivery"},
        "execution": {"tenant_id": "DGE", "user_id": "U1", "correlation_id": "REQ-1"},
    }
    transport = httpx.ASGITransport(app=create_app(platform))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/agents/run", json=body)
    finally:
        await platform.close()

    assert response.status_code == 200
    assert response.json()["trace_name"] == "candidate-job-scorer-Delivery"


async def test_the_api_refuses_a_name_or_field_it_cannot_record(platform):
    import httpx

    from asas_agent.api.app import create_app

    body = {
        "agent_key": "scorer",
        "environment": "dev",
        "input": "hi",
        "trace_metadata": {"agent_version": 99},
        "execution": {"tenant_id": "DGE", "user_id": "U1", "correlation_id": "REQ-1"},
    }
    transport = httpx.ASGITransport(app=create_app(platform))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/v1/agents/run", json=body)
    finally:
        await platform.close()

    assert response.status_code == 400
    assert "agent_version" in response.json()["detail"]


# ----- a name is read by people and by tools -------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "scorer\x1b[2Jwiped",  # An escape sequence that rewrites a terminal.
        "scorer\x00",
        "scorer-‮yrevileD",  # A direction override, which reverses what is displayed.
        "scor​er",  # A zero-width space, which hides a difference.
    ],
)
async def test_a_name_that_would_deceive_a_reader_is_refused(platform, name):
    try:
        with pytest.raises(RunInputError, match="control or formatting"):
            await run(platform, trace_name=name)
    finally:
        await platform.close()


async def test_a_request_cannot_name_its_run_after_another_agent(platform):
    """`agent:<key>` is how the runtime names a run, and a harness reads it as such."""
    try:
        with pytest.raises(RunInputError, match="cannot start with"):
            await run(platform, trace_name="agent:payroll-approver")
    finally:
        await platform.close()


async def test_a_name_is_measured_in_bytes_because_that_is_what_is_stored(platform):
    try:
        assert (await run(platform, trace_name="é" * 100)).trace_name == "é" * 100  # 200 bytes.
        with pytest.raises(RunInputError, match="at most 200 bytes"):
            await run(platform, trace_name="é" * 101)
    finally:
        await platform.close()


async def test_trace_metadata_larger_than_the_ceiling_is_refused(platform):
    try:
        with pytest.raises(RunInputError, match="trace metadata"):
            await run(platform, trace_metadata={"document": "x" * 20_000})
    finally:
        await platform.close()


# ----- what the runtime records is its own ---------------------------------------------


def test_every_field_the_factory_records_is_reserved():
    """The two lists live in different files; this is what keeps them together."""
    import ast
    from pathlib import Path

    from asas_agent.runtime.runner import RESERVED_TRACE_KEYS

    source = Path("src/asas_agent/runtime/factory.py").read_text(encoding="utf-8")
    written = {
        key.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
        for argument in node.args
        if isinstance(argument, ast.Dict)
        for key in argument.keys
        if isinstance(key, ast.Constant)
    }

    assert written, "the factory no longer records trace metadata where this test looks for it"
    assert written <= RESERVED_TRACE_KEYS


@pytest.mark.parametrize("key", ["agent_version", "tenant_id"])
async def test_the_application_cannot_rewrite_them_either(platform, key):
    """Not only a request: an embedding application's context is checked the same way."""
    try:
        with pytest.raises(RunInputError, match=key):
            await platform.runtime.run(
                agent_key="scorer",
                environment="dev",
                user_input="hi",
                context=context(trace_metadata={key: "forged"}),
            )
    finally:
        await platform.close()


async def test_a_call_is_more_specific_than_the_application_it_runs_in(platform):
    tracer = Tracer()
    platform.runtime._tracer = tracer
    try:
        await platform.runtime.run(
            agent_key="scorer",
            environment="dev",
            user_input="hi",
            trace_metadata={"area": "Delivery"},
            context=context(trace_metadata={"area": "unset", "deployment": "blue"}),
        )
    finally:
        await platform.close()

    metadata = tracer.traces[0]["metadata"]
    assert metadata["area"] == "Delivery"
    assert metadata["deployment"] == "blue"


async def test_a_fan_out_sharing_one_context_keeps_its_branches_apart(platform):
    """Forty runs of one agent, one context object between them."""
    import asyncio

    tracer = Tracer()
    platform.runtime._tracer = tracer
    shared = context()
    try:
        await asyncio.gather(
            *(
                platform.runtime.run(
                    agent_key="scorer",
                    environment="dev",
                    user_input="hi",
                    trace_name=f"candidate-job-scorer-{i}",
                    trace_metadata={"area": f"area-{i}"},
                    context=shared,
                )
                for i in range(40)
            )
        )
    finally:
        await platform.close()

    assert sorted(tracer.observations) == sorted(f"candidate-job-scorer-{i}" for i in range(40))
    recorded = {trace["metadata"]["area"] for trace in tracer.traces}
    assert recorded == {f"area-{i}" for i in range(40)}
    assert shared.trace_metadata == {}


# ----- the interfaces around a run -----------------------------------------------------


def test_the_cli_names_a_run_and_says_what_it_used(monkeypatch):
    from typer.testing import CliRunner

    from asas_agent.cli.main import app as cli_app

    run_agent = AsyncMock(
        return_value=SimpleNamespace(
            output="done",
            agent_version=2,
            prompt_version=None,
            trace_id="TRACE-1",
            trace_name="score summarizer",
            toolset=[],
        )
    )
    stub = SimpleNamespace(runtime=SimpleNamespace(run=run_agent), close=AsyncMock())
    monkeypatch.setattr("asas_agent.cli.main._platform", lambda *a, **k: stub)

    result = CliRunner().invoke(cli_app, ["run", "scorer", "hi", "--trace-name", "score summarizer"])
    assert result.exit_code == 0, result.output
    assert run_agent.call_args.kwargs["trace_name"] == "score summarizer"
    # A name may contain spaces, so the id has to stay separable.
    assert "trace 'score summarizer' (TRACE-1)" in result.output


def test_the_cli_reports_an_unusable_name_as_a_bad_argument(monkeypatch):
    from typer.testing import CliRunner

    from asas_agent.cli.main import app as cli_app

    stub = SimpleNamespace(
        runtime=SimpleNamespace(run=AsyncMock(side_effect=RunInputError("blank"))), close=AsyncMock()
    )
    monkeypatch.setattr("asas_agent.cli.main._platform", lambda *a, **k: stub)

    result = CliRunner().invoke(cli_app, ["run", "scorer", "hi", "--trace-name", "   "])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_choosing_an_internal_trace_backend_never_leaves_an_external_one_installed(settings, monkeypatch):
    """`instrument()` reports a version mismatch by logging, so a caller must check, not assume."""
    import sys

    from agents.tracing import get_trace_provider

    from asas_agent.bootstrap import _build_tracer

    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"

    class Instrumentor:
        def instrument(self, **kwargs):
            return None  # Attached to nothing, and said so only in a log line.

    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.openai_agents",
        SimpleNamespace(OpenAIAgentsInstrumentor=Instrumentor),
    )

    _build_tracer(settings)

    processors = get_trace_provider()._multi_processor._processors
    assert processors == (), "the SDK's own exporter was left in place"
