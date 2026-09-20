"""The model a definition names, through the real SDK, to a typed result.

There is no table of what each model can do any more: a model that cannot
return structured output is refused by its provider, in its own words. What
this package still owes a caller is that a definition naming a model and an
output schema produces a validated object - so that is what is tested here,
over a transport that answers instead of a model that costs money.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from agents import Runner
from pydantic import BaseModel

from asas_agent.bootstrap import build_platform
from asas_agent.config import Settings
from asas_agent.integrations.models import ModelError, ModelRegistry
from asas_agent.registry.outputs import OutputSchemaRegistry
from asas_agent.registry.repository import AgentDefinition
from asas_agent.registry.schema import AgentConfig
from asas_agent.runtime.context import RuntimeContext


class Assessment(BaseModel):
    recommendation: str
    evidence: list[str]


ANSWER = {"recommendation": "shortlist", "evidence": ["eight years of Python", "led two migrations"]}


@pytest.fixture
def settings(monkeypatch):
    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql://test@localhost/test",
        OPENAI_API_KEY="test-key",
    )


def definition(config: AgentConfig) -> AgentDefinition:
    return AgentDefinition(
        agent_key="assessor",
        version=1,
        status="published",
        config=config,
        created_at=datetime.now(UTC),
        created_by="test",
        published_at=datetime.now(UTC),
    )


def responses_api(body: str):
    """A transport that answers a Responses API call with the text it is given."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
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
                        "content": [{"type": "output_text", "text": body, "annotations": []}],
                    }
                ],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
            },
        )

    return respond, requests


async def run_against(platform, transport_requests, config, monkeypatch, **run_kwargs):
    monkeypatch.setattr(
        platform.repository._repository,
        "get_active",
        _always(definition(config)),
    )
    return await platform.runtime.run(
        agent_key="assessor",
        environment="dev",
        context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        **run_kwargs,
    )


async def answer_with(platform, respond) -> None:
    """Point the real OpenAI client at a transport that answers, so no request leaves."""
    platform.models.resolve("openai", "gpt-5-nano")
    sdk_client = platform.models._clients["openai"]
    await sdk_client._client.aclose()
    sdk_client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))


def _always(value):
    async def get_active(**kwargs):
        return value

    return get_active


def config(**overrides) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "name": "Assessor",
            "prompt": {"name": "assessor", "snapshot": "Assess the candidate in {{area}}."},
            "model": {"provider": "openai", "name": "gpt-5-nano"},
            "output": {"schema": "assessment_v1"},
            **overrides,
        }
    )


@pytest.fixture
def platform(settings, monkeypatch):
    registry = OutputSchemaRegistry()
    registry.register("assessment_v1", Assessment)
    monkeypatch.setattr("asas_agent.bootstrap.output_module.registry", registry)
    built = build_platform(settings)
    monkeypatch.setattr(built.runtime.factory, "outputs", registry)
    return built


async def test_a_definition_naming_a_model_and_a_schema_returns_a_validated_object(platform, monkeypatch):
    respond, requests = responses_api(json.dumps(ANSWER))
    await answer_with(platform, respond)

    try:
        result = await run_against(
            platform, requests, config(), monkeypatch, inputs={"area": "delivery"}, message="Assess."
        )
    finally:
        await platform.close()

    # The typed result, not a string that happens to look like JSON.
    assert isinstance(result.output, Assessment)
    assert result.output.recommendation == "shortlist"
    assert result.output.evidence[0] == "eight years of Python"

    sent = json.loads(requests[0].content)
    assert requests[0].headers["authorization"] == "Bearer test-key"
    # The definition's prompt and schema reached the model.
    assert "Assess the candidate in delivery." in json.dumps(sent)
    assert sent["text"]["format"]["type"] == "json_schema"


async def test_an_answer_that_does_not_fit_the_schema_is_an_error_not_a_result(platform, monkeypatch):
    """A model returning the wrong shape must not reach the caller as one."""
    respond, requests = responses_api(json.dumps({"recommendation": "shortlist"}))  # `evidence` missing
    await answer_with(platform, respond)

    try:
        with pytest.raises(Exception) as raised:
            await run_against(platform, requests, config(), monkeypatch, inputs={"area": "delivery"}, message="Assess.")
    finally:
        await platform.close()

    assert "evidence" in str(raised.value) or "validation" in str(raised.value).lower()


async def test_an_unknown_provider_is_refused_before_anything_is_sent(settings):
    with pytest.raises(ModelError, match="Unknown model provider"):
        ModelRegistry(settings=settings).resolve("sovereign", "some-model")


async def test_a_model_setting_the_sdk_does_not_take_is_refused(settings):
    registry = ModelRegistry(settings=settings)
    assert registry.resolve_settings({"reasoning_effort": "low"}) == {"reasoning": {"effort": "low"}}
    with pytest.raises(ModelError, match="Unknown model settings"):
        registry.resolve_settings({"temperature_c": 20})


async def test_a_gateway_model_needs_its_url(settings):
    settings.gateway_base_url = ""
    with pytest.raises(ModelError, match="MODEL_GATEWAY_URL"):
        ModelRegistry(settings=settings).resolve("gateway", "jais")


async def test_the_runner_really_ran(platform, monkeypatch):
    """Guard against the test passing because the SDK was stubbed out from under it."""
    assert Runner.run.__module__.startswith("agents")
    await platform.close()


def test_the_output_registry_refuses_a_schema_nobody_registered():
    from asas_agent.registry.outputs import OutputSchemaError

    with pytest.raises(OutputSchemaError, match="Unknown output schema"):
        OutputSchemaRegistry().resolve("not_registered_v1")


def test_two_schemas_cannot_share_a_name():
    from asas_agent.registry.outputs import OutputSchemaError

    registry = OutputSchemaRegistry()
    registry.register("assessment_v1", Assessment)
    with pytest.raises(OutputSchemaError, match="already registered"):
        registry.register("assessment_v1", SimpleNamespace)
