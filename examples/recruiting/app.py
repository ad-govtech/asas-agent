"""A recruiting console that exercises asas-agent.

Pick an agent, pick a case, change the model or the tools, run it, and see what
actually ran: which version, which prompt version, which tools were available.

    uv run python -m examples.recruiting.seed
    uv run python -m examples.recruiting.app      # http://localhost:8010

Without OPENAI_API_KEY the console still resolves the real definition from the
registry and shows a clearly marked sample answer, so the mechanics can be
demonstrated before a model key exists.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from asas_agent import CapabilityPolicy, RuntimeContext, build_platform
from asas_agent.registry.capabilities import registry as capability_registry
from asas_agent.registry.outputs import registry as output_registry
from asas_agent.registry.repository import RegistryError

from . import capabilities as _capabilities  # noqa: F401  registers the tools
from . import schemas as _schemas  # noqa: F401  registers the output schemas
from .domain import application_view, scenarios
from .samples import sample_output

STATIC = Path(__file__).parent / "static"
ENVIRONMENT = "dev"

_platform: Any = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _platform
    _platform = build_platform()
    yield
    await _platform.close()


app = FastAPI(title="Recruiting console", summary="A demo app on top of asas-agent.", lifespan=lifespan)


class RunRequest(BaseModel):
    agent_key: str
    scenario_id: str
    request: str
    tools: list[str] = Field(default_factory=list, description="Tools this caller allows, from the agent's own list.")
    allow_action_tools: bool = False
    max_turns: int = 6


class VariantRequest(BaseModel):
    agent_key: str
    model_provider: str
    model_name: str
    temperature: float | None = None
    tools: list[str] = Field(default_factory=list)
    max_turns: int = 6
    promote: bool = True


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/options")
async def options() -> dict[str, Any]:
    """Everything the dropdowns need, read from the registry."""
    # Only this example's agents. Other products share the registry.
    ours = {path.stem for path in (Path(__file__).parent / "agents").glob("*.json")}
    bindings = {
        b["agent_key"]: b
        for b in await _platform.repository.bindings()
        if b["environment"] == ENVIRONMENT and b["agent_key"] in ours
    }
    agents = []

    for agent_key in sorted(bindings):
        definition = await _platform.repository.get_active(agent_key=agent_key, environment=ENVIRONMENT)
        config = definition.config
        agents.append(
            {
                "agent_key": agent_key,
                "name": config.name,
                "description": config.description,
                "version": definition.version,
                "model": f"{config.model.provider}:{config.model.name}",
                "tools": config.tools,
                "output_schema": config.output.schema_key,
                "max_turns": config.runtime.max_turns,
            }
        )

    models = ["openai:gpt-5.6-sol", "openai:gpt-5-nano"]
    if _platform.settings.gateway_base_url:
        models.append("gateway:jais")

    return {
        "agents": agents,
        "scenarios": scenarios(),
        "models": models,
        "capabilities": capability_registry.keys(),
        "output_schemas": output_registry.keys(),
        "environment": ENVIRONMENT,
        "model_key_set": bool(_platform.settings.openai_api_key or _platform.settings.gateway_api_key),
        "tracing": _platform.settings.langfuse_configured,
    }


@app.post("/api/run")
async def run(request: RunRequest) -> dict[str, Any]:
    try:
        definition = await _platform.repository.get_active(agent_key=request.agent_key, environment=ENVIRONMENT)
    except RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    context_facts = application_view(request.scenario_id)
    started = time.perf_counter()

    runtime_context = RuntimeContext(
        tenant_id="DGE",
        user_id="recruiter-demo",
        correlation_id=f"demo-{int(started)}",
        environment=ENVIRONMENT,
        max_turns=request.max_turns,
        policy=CapabilityPolicy(
            allowed=set(request.tools) if request.tools else set(),
            allow_action_tools=request.allow_action_tools,
        ),
    )

    has_key = bool(_platform.settings.openai_api_key or _platform.settings.gateway_api_key)

    if not has_key:
        prompt = await _platform.prompts.resolve(definition.config.prompt)
        return {
            "mode": "sample",
            "output": sample_output(definition.config.output.schema_key, request.scenario_id),
            "ran": {
                "agent_key": request.agent_key,
                "agent_version": definition.version,
                "prompt_name": prompt.name,
                "prompt_version": prompt.version,
                "model": f"{definition.config.model.provider}:{definition.config.model.name}",
                "tools_configured": definition.config.tools,
                "tools_allowed": request.tools,
                "max_turns": min(request.max_turns, definition.config.runtime.max_turns),
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "trace_id": None,
            },
            "context": context_facts,
        }

    result = await _platform.runtime.run(
        agent_key=request.agent_key,
        environment=ENVIRONMENT,
        user_input=request.request,
        business_context=context_facts,
        context=runtime_context,
    )

    output = result.output
    return {
        "mode": "model",
        "output": output.model_dump() if hasattr(output, "model_dump") else output,
        "ran": {
            "agent_key": result.agent_key,
            "agent_version": result.agent_version,
            "prompt_name": definition.config.prompt.name,
            "prompt_version": result.prompt_version,
            "model": f"{definition.config.model.provider}:{definition.config.model.name}",
            "tools_configured": result.toolset,
            "tools_allowed": request.tools,
            "max_turns": min(request.max_turns, definition.config.runtime.max_turns),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "trace_id": result.trace_id,
        },
        "context": context_facts,
    }


@app.post("/api/variant")
async def variant(request: VariantRequest) -> dict[str, Any]:
    """Save the chosen settings as a new agent version, and make it live.

    This is the point of the platform: changing a model or a toolset is a new
    version and a moved binding, not a deployment.
    """
    definition = await _platform.repository.get_active(agent_key=request.agent_key, environment=ENVIRONMENT)
    config = definition.config.model_copy(deep=True)

    config.model.provider = request.model_provider
    config.model.name = request.model_name
    config.model.settings = {"temperature": request.temperature} if request.temperature is not None else {}
    config.tools = request.tools
    config.runtime.max_turns = request.max_turns

    created = await _platform.repository.create_draft(
        agent_key=request.agent_key, config=config, created_by="recruiting-demo"
    )
    await _platform.repository.publish(agent_key=request.agent_key, version=created.version)

    if request.promote:
        await _platform.repository.bind(
            agent_key=request.agent_key,
            environment=ENVIRONMENT,
            version=created.version,
            updated_by="recruiting-demo",
        )

    return {
        "agent_key": request.agent_key,
        "version": created.version,
        "live": request.promote,
        "model": f"{config.model.provider}:{config.model.name}",
        "tools": config.tools,
    }


@app.get("/api/versions/{agent_key}")
async def versions(agent_key: str) -> dict[str, Any]:
    definitions = await _platform.repository.list_versions(agent_key)
    live = {
        b["agent_key"]: b["agent_version"]
        for b in await _platform.repository.bindings()
        if b["environment"] == ENVIRONMENT
    }
    return {
        "versions": [
            {
                "version": d.version,
                "status": d.status,
                "model": f"{d.config.model.provider}:{d.config.model.name}",
                "tools": d.config.tools,
                "live": live.get(agent_key) == d.version,
            }
            for d in definitions
        ]
    }


@app.post("/api/rollback/{agent_key}/{version}")
async def rollback(agent_key: str, version: int) -> dict[str, Any]:
    await _platform.repository.bind(
        agent_key=agent_key, environment=ENVIRONMENT, version=version, updated_by="recruiting-demo"
    )
    return {"agent_key": agent_key, "version": version, "live": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("examples.recruiting.app:app", host="127.0.0.1", port=8010, reload=False)
