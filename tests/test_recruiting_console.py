"""The recruiting console, driven through its own endpoints.

The console is how the package is demonstrated, and its most-used path is the
one without model credentials: it resolves the real definition and the real
prompt, then shows a sample answer. That path has to survive changes to how a
prompt is filled, which is what these tests hold on to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from asas_agent.integrations.prompts import FilePrompts
from asas_agent.registry.repository import AgentDefinition
from asas_agent.registry.schema import AgentConfig

AGENTS = Path("examples/recruiting/agents")
PROMPTS = Path("examples/recruiting/prompts")


def definition(agent_key: str) -> AgentDefinition:
    """The agent as the example ships it, published as the seed script would."""
    config = AgentConfig.model_validate(json.loads((AGENTS / f"{agent_key}.json").read_text(encoding="utf-8")))
    return AgentDefinition(
        agent_key=agent_key,
        version=1,
        status="published",
        config=config,
        created_at=datetime.now(UTC),
        created_by="test",
        published_at=datetime.now(UTC),
    )


@pytest.fixture
def console(monkeypatch):
    """The console app with no model credentials, on the example's own prompts."""
    from examples.recruiting import app as console_app

    platform = SimpleNamespace(
        settings=SimpleNamespace(openai_api_key="", gateway_api_key="", langfuse_configured=False),
        prompts=FilePrompts(PROMPTS),
        repository=SimpleNamespace(
            get_active=AsyncMock(side_effect=lambda *, agent_key, environment: definition(agent_key)),
            bindings=AsyncMock(return_value=[]),
        ),
        runtime=SimpleNamespace(run=AsyncMock()),
    )
    monkeypatch.setattr(console_app, "_platform", platform)
    return console_app


@pytest.mark.parametrize("agent_key", sorted(path.stem for path in AGENTS.glob("*.json")))
async def test_every_shipped_agent_answers_without_a_model_key(console, agent_key):
    """This is what someone sees on their first run, before they have any credentials."""
    body = {"agent_key": agent_key, "scenario_id": "APP-7781", "request": "Assess the application", "tools": []}
    transport = httpx.ASGITransport(app=console.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/run", json=body)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "sample"
    assert payload["output"]
    # The real prompt was resolved, not a stand-in.
    assert payload["ran"]["prompt_name"] == definition(agent_key).config.prompt.name


async def test_the_sample_path_fills_the_prompt_the_same_way_a_run_would(console):
    """A prompt asking for a value it is never given is the bug this covers."""
    resolved = await console._platform.prompts.resolve(
        definition("screening-assistant").config.prompt,
        {"case": {"application": {"id": "APP-7781"}}},
    )
    assert "{{case}}" not in resolved.instructions
    assert "APP-7781" in resolved.instructions
