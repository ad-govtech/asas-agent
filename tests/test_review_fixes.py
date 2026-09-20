"""Regressions for the repository-wide review, including a real SDK run."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agents import Agent, Model, ModelResponse, ModelSettings, RunConfig, Runner
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from asas_agent.api.app import create_app
from asas_agent.bootstrap import build_platform
from asas_agent.cli.main import app as cli_app
from asas_agent.config import Settings, normalize_dsn
from asas_agent.integrations.models import ModelError, ModelRegistry
from asas_agent.integrations.prompts import LangfusePrompts
from asas_agent.registry.schema import AgentConfig, PromptRef
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.factory import BuiltAgent


@pytest.fixture
def settings(monkeypatch):
    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    return Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test", OPENAI_API_KEY="test-key")


def config(**overrides):
    return AgentConfig.model_validate(
        {
            "name": "Test",
            "prompt": {"name": "test", "snapshot": "Instructions"},
            "model": {"provider": "openai", "name": "gpt-5-nano"},
            **overrides,
        }
    )


def context(**overrides):
    return RuntimeContext(tenant_id="T", user_id="U", correlation_id="R", **overrides)


class LocalModel(Model):
    """Runs through the real SDK without sending requests to a model service."""

    async def get_response(self, *args, **kwargs):
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="msg_test",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
                    type="message",
                )
            ],
            usage=Usage(),
            response_id="resp_test",
        )

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield  # Required by the streaming interface.


async def test_real_sdk_runner_with_locked_dependencies():
    result = await Runner.run(Agent(name="test", model=LocalModel()), "hi", run_config=RunConfig(tracing_disabled=True))
    assert result.final_output == "done"
    assert result.context_wrapper.usage.requests == 0


async def test_api_auth_uses_injected_platform_settings(settings, monkeypatch):
    settings.api_key = "platform-key"
    platform = build_platform(settings)
    monkeypatch.setattr(platform.repository, "bindings", AsyncMock(return_value=[]))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(platform)), base_url="http://test"
        ) as client:
            assert (await client.get("/v1/agents")).status_code == 401
            assert (await client.get("/v1/agents", headers={"X-API-Key": "wrong"})).status_code == 401
            assert (await client.get("/v1/agents", headers={"X-API-Key": "platform-key"})).status_code == 200
    finally:
        await platform.close()


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "require", "verify-ca", "verify-full"])
def test_ssl_modes_survive_url_normalization(mode):
    url = make_url(normalize_dsn(f"postgresql://u:pa%40ss@host/db?sslmode={mode}&application_name=review"))
    assert url.query["ssl"] == mode
    assert url.query["application_name"] == "review"
    assert url.password == "pa@ss"


def test_conflicting_tls_options_are_rejected():
    with pytest.raises(ValueError, match="Conflicting"):
        normalize_dsn("postgres://u@host/db?sslmode=verify-full&ssl=disable")


def test_migrate_preserves_encoded_password(settings, monkeypatch):
    settings.database_url = "postgresql+asyncpg://u:pa%40ss%25@host/db"
    monkeypatch.setattr("asas_agent.cli.main.get_settings", lambda: settings)
    upgrade = MagicMock()
    monkeypatch.setattr("alembic.command.upgrade", upgrade)
    result = CliRunner().invoke(cli_app, ["migrate"])
    assert result.exit_code == 0, result.output
    assert upgrade.call_args.args[0].get_main_option("sqlalchemy.url") == settings.database_url


async def test_dotenv_key_reaches_real_openai_client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=dotenv-test-key\n")
    registry = ModelRegistry(Settings(_env_file=env_file))
    model = registry.resolve("openai", "gpt-5-nano")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-5-nano",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    sdk_client = registry._clients["openai"]
    await sdk_client._client.aclose()
    sdk_client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await Runner.run(Agent(name="test", model=model), "hi", run_config=RunConfig(tracing_disabled=True))
        assert result.final_output == "ok"
        assert requests[0].headers["authorization"] == "Bearer dotenv-test-key"
    finally:
        await registry.close()


def test_reasoning_alias_is_translated(settings):
    translated = ModelRegistry(settings).resolve_settings({"reasoning_effort": "low"})
    assert ModelSettings(**translated).reasoning.effort == "low"


@pytest.mark.parametrize(
    "values", [{"reasoning_effort": "invalid"}, {"unknown": 1}, {"reasoning": {}, "reasoning_effort": "low"}]
)
def test_invalid_model_settings_fail_validation(settings, values):
    with pytest.raises(ModelError):
        ModelRegistry(settings).resolve_settings(values)


async def test_direct_runtime_merges_dependencies_and_clamps_limits(settings, monkeypatch):
    settings.max_turns_ceiling = 2
    platform = build_platform(settings, dependencies={"service": "default", "other": "shared"})
    request_context = context(max_turns=100, dependencies={"service": "request"})
    monkeypatch.setattr(
        platform.repository,
        "get_active",
        AsyncMock(
            return_value=SimpleNamespace(
                version=1,
                config=config(runtime={"max_turns": 50, "timeout_seconds": 100}),
            )
        ),
    )
    run = AsyncMock(return_value=SimpleNamespace(final_output="done"))
    monkeypatch.setattr(Runner, "run", run)
    try:
        await platform.runtime.run(agent_key="test", environment="dev", message="hi", context=request_context)
        actual = run.call_args.kwargs
        assert actual["max_turns"] == 2
        assert actual["context"].dependency("service") == "request"
        assert actual["context"].dependency("other") == "shared"
        assert request_context.dependencies == {"service": "request"}
        assert request_context.max_turns == 100
    finally:
        await platform.close()


@pytest.mark.parametrize("phase", ["assembly", "execution"])
async def test_runtime_deadline_cancels_pending_work(settings, monkeypatch, phase):
    platform = build_platform(settings)
    cancelled = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        platform.repository, "get_active", AsyncMock(return_value=SimpleNamespace(version=1, config=config()))
    )
    if phase == "assembly":
        monkeypatch.setattr(platform.runtime.factory, "build", wait_forever)
    else:
        # Hand the runtime an agent that is already assembled, so the deadline
        # is the execution's alone. Otherwise a slow machine can spend the
        # whole budget building one, and the run times out before it starts.
        prepared = BuiltAgent(
            agent=SimpleNamespace(),
            agent_version=1,
            prompt_version=None,
            config=config(),
            max_turns=2,
            timeout_seconds=0.02,
            prompt_messages=(),
        )
        monkeypatch.setattr(platform.runtime.factory, "build", AsyncMock(return_value=prepared))
        monkeypatch.setattr(Runner, "run", wait_forever)
    try:
        with pytest.raises(TimeoutError):
            await platform.runtime.run(
                agent_key="test", environment="dev", message="hi", context=context(timeout_seconds=0.02)
            )
        assert cancelled.is_set()
    finally:
        await platform.close()


async def test_langfuse_calls_do_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()

    class Client:
        def get_prompt(self, *args, **kwargs):
            started.set()
            release.wait(timeout=1)
            return SimpleNamespace(version=1, prompt="prompt", compile=lambda: "prompt")

    task = asyncio.create_task(LangfusePrompts(Client()).resolve(PromptRef(name="test")))
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        await asyncio.sleep(0.01)
        assert not task.done()  # The loop remained responsive while the call was blocked.
    finally:
        release.set()
        await task


async def test_trace_flush_runs_off_event_loop(settings):
    platform = build_platform(settings)
    main_thread = threading.get_ident()
    flushed = []

    class Tracer:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            yield SimpleNamespace(trace_id="test")

        def update_current_trace(self, **kwargs):
            pass

        def flush(self):
            flushed.append(threading.get_ident())

    platform.runtime._tracer = Tracer()
    try:
        async with platform.runtime._trace("test", "test", context()):
            pass
        assert flushed and flushed[0] != main_thread
    finally:
        await platform.close()


async def test_cancelled_trace_does_not_wait_for_flush(settings):
    platform = build_platform(settings)
    tracer = MagicMock()
    tracer.start_as_current_observation.return_value.__enter__.return_value.trace_id = "test"
    platform.runtime._tracer = tracer
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                async with platform.runtime._trace("test", "test", context()):
                    await asyncio.Event().wait()
        tracer.flush.assert_not_called()
    finally:
        await platform.close()


async def test_api_reports_deadline_as_gateway_timeout(settings, monkeypatch):
    platform = build_platform(settings)
    monkeypatch.setattr(platform.runtime, "run", AsyncMock(side_effect=TimeoutError))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(platform)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/agents/run",
                json={
                    "agent_key": "test",
                    "input": "hi",
                    "execution": {"tenant_id": "T", "user_id": "U", "correlation_id": "R"},
                },
            )
            assert response.status_code == 504
    finally:
        await platform.close()


async def test_real_runtime_stops_tool_loop_at_platform_ceiling(settings, monkeypatch):
    from agents import function_tool
    from agents.exceptions import MaxTurnsExceeded
    from openai.types.responses import ResponseFunctionToolCall

    from asas_agent.registry.capabilities import CapabilityRegistry

    class LoopModel(LocalModel):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        id=f"fc_{self.calls}",
                        call_id=f"call_{self.calls}",
                        name="again",
                        arguments="{}",
                        type="function_call",
                    )
                ],
                usage=Usage(),
                response_id=f"resp_{self.calls}",
            )

    settings.max_turns_ceiling = 2
    platform = build_platform(settings, dependencies={"service": "registered"})
    model = LoopModel()
    seen = []
    capabilities = CapabilityRegistry()

    @capabilities.capability("again")
    def make_tool(runtime_context):
        @function_tool
        async def again() -> str:
            """Return context to the local test model."""
            seen.append(runtime_context.dependency("service"))
            return "again"

        return again

    platform.runtime.factory.capabilities = capabilities
    monkeypatch.setattr(platform.models, "resolve", lambda *args: model)
    monkeypatch.setattr(
        platform.repository,
        "get_active",
        AsyncMock(
            return_value=SimpleNamespace(
                version=1,
                config=config(tools=["again"], runtime={"max_turns": 20}),
            )
        ),
    )
    try:
        with pytest.raises(MaxTurnsExceeded):
            await platform.runtime.run(agent_key="test", environment="dev", message="hi", context=context(max_turns=20))
        assert model.calls == 2
        assert seen == ["registered", "registered"]
    finally:
        await platform.close()


async def test_definition_deadline_also_bounds_prompt_resolution(settings, monkeypatch):
    platform = build_platform(settings)
    cancelled = asyncio.Event()

    async def blocked_prompt(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        platform.repository,
        "get_active",
        AsyncMock(
            return_value=SimpleNamespace(
                version=1,
                config=config(runtime={"timeout_seconds": 1}),
            )
        ),
    )
    monkeypatch.setattr(platform.prompts, "resolve", blocked_prompt)
    run = AsyncMock()
    monkeypatch.setattr(Runner, "run", run)
    try:
        with pytest.raises(TimeoutError):
            await platform.runtime.run(agent_key="test", environment="dev", message="hi", context=context())
        assert cancelled.is_set()
        run.assert_not_called()
    finally:
        await platform.close()
