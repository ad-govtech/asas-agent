"""The stateless runtime, over HTTP.

One endpoint runs an agent. The caller is a business service that has already
authenticated its user and loaded the context the agent needs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Any

from agents.exceptions import MaxTurnsExceeded
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from asas_agent.bootstrap import Platform, build_platform
from asas_agent.integrations.models import ModelError
from asas_agent.integrations.prompts import PromptError, PromptShapeError, PromptVariableError
from asas_agent.registry.capabilities import CapabilityError
from asas_agent.registry.outputs import OutputSchemaError
from asas_agent.registry.repository import RegistryError
from asas_agent.runtime.context import CapabilityPolicy, RuntimeContext
from asas_agent.runtime.runner import RunInputError


class ExecutionContext(BaseModel):
    tenant_id: str
    user_id: str
    correlation_id: str
    access_token: str | None = Field(default=None, description="The caller's token, passed on to business APIs.")
    allow_action_tools: bool = False


class AgentRunRequest(BaseModel):
    agent_key: str
    environment: str = "production"
    input: str = ""
    context: dict[str, Any] = Field(default_factory=dict, description="Facts the business service already loaded.")
    prompt_variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Values for this request's `{{placeholders}}`, on top of the ones the definition sets.",
    )
    trace_name: str | None = Field(
        default=None,
        description="What to call this run in the trace, for telling apart many runs of one agent.",
    )
    trace_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Fields to record on the trace, alongside what the runtime records itself.",
    )
    execution: ExecutionContext


class AgentRunResponse(BaseModel):
    output: Any
    agent_key: str
    agent_version: int
    prompt_version: int | None
    trace_id: str | None
    trace_name: str
    toolset: list[str]


def create_app(platform: Platform | None = None) -> FastAPI:
    state: dict[str, Platform | None] = {"platform": platform}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["platform"] is None:
            state["platform"] = build_platform()
        yield
        if platform is None and state["platform"] is not None:
            await state["platform"].close()

    app = FastAPI(
        title="asas-agent runtime",
        version="0.1.0",
        summary="Runs versioned agent definitions. Stateless, so scale it horizontally.",
        lifespan=lifespan,
    )

    def get_platform() -> Platform:
        current = state["platform"]
        if current is None:
            raise HTTPException(status_code=503, detail="The runtime is still starting")
        return current

    async def authorize(x_api_key: str | None = Header(default=None)) -> None:
        expected = get_platform().settings.api_key
        if expected and not compare_digest((x_api_key or "").encode(), expected.encode()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Send a valid X-API-Key header")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/agents", dependencies=[Depends(authorize)])
    async def list_agents(platform: Platform = Depends(get_platform)) -> dict[str, Any]:
        return {"bindings": await platform.repository.bindings()}

    @app.post("/v1/agents/run", response_model=AgentRunResponse, dependencies=[Depends(authorize)])
    async def run_agent(
        request: AgentRunRequest,
        platform: Platform = Depends(get_platform),
    ) -> AgentRunResponse:
        context = RuntimeContext(
            tenant_id=request.execution.tenant_id,
            user_id=request.execution.user_id,
            correlation_id=request.execution.correlation_id,
            access_token=request.execution.access_token,
            environment=request.environment,
            policy=CapabilityPolicy(allow_action_tools=request.execution.allow_action_tools),
            dependencies=getattr(platform.runtime, "default_dependencies", {}),
            max_turns=platform.settings.max_turns_ceiling,
            timeout_seconds=platform.settings.timeout_ceiling_seconds,
        )

        try:
            result = await platform.runtime.run(
                agent_key=request.agent_key,
                environment=request.environment,
                user_input=request.input,
                business_context=request.context,
                prompt_variables=request.prompt_variables,
                trace_name=request.trace_name,
                trace_metadata=request.trace_metadata,
                context=context,
            )
        except RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CapabilityError, OutputSchemaError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (PromptVariableError, RunInputError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PromptShapeError as exc:
            # The definition is broken, not the prompt service.
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except (PromptError, ModelError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="The agent exceeded its execution deadline") from exc
        except MaxTurnsExceeded as exc:
            raise HTTPException(status_code=422, detail="The agent exceeded its turn limit") from exc

        return AgentRunResponse(
            output=result.output,
            agent_key=result.agent_key,
            agent_version=result.agent_version,
            prompt_version=result.prompt_version,
            trace_id=result.trace_id,
            trace_name=result.trace_name,
            toolset=result.toolset,
        )

    return app


app = create_app()
