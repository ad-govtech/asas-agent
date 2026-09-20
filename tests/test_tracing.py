"""What a run is called, and what is recorded about it.

One agent is often run many times over in a second - once per rubric area, once
per candidate - and afterwards someone has to tell those runs apart: a person
reading traces, or an evaluation harness that groups generations by name.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agents import Runner

from asas_agent.bootstrap import build_platform
from asas_agent.config import Settings
from asas_agent.registry.repository import AgentDefinition
from asas_agent.registry.schema import AgentConfig
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.runner import TRACE_NAME_MAX_LENGTH, RunInputError


class Tracer:
    """A stand-in Langfuse client that records what it was told."""

    def __init__(self):
        self.observations: list[str] = []
        self.traces: list[dict] = []
        self.flushes = 0

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        self.observations.append(kwargs.get("name"))
        yield SimpleNamespace(trace_id="trace-1")

    def update_current_trace(self, **kwargs):
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


async def test_the_name_reaches_the_sdks_own_trace_too(platform):
    try:
        result = await run(platform, trace_name="score-summarizer")
    finally:
        await platform.close()

    run_config = Runner.run.call_args.kwargs["run_config"]
    assert run_config.workflow_name == "score-summarizer"
    assert run_config.group_id == "REQ-1"  # The correlation id groups one request's runs.
    assert result.trace_name == "score-summarizer"


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


@pytest.mark.parametrize("name", ["", "   ", "x" * (TRACE_NAME_MAX_LENGTH + 1)])
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
