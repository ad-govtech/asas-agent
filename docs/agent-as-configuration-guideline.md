# Agent-as-Configuration Platform
## Engineering Guideline and Working Prototype Blueprint

**Status:** Proposed architecture  
**Audience:** Platform, backend, AI/ML and application engineers  
**Primary stack:** Python, OpenAI Agents SDK, PostgreSQL, Langfuse  
**Goal:** Build a stateless agent runtime in which agents are assembled at runtime from configuration rather than hard-coded and deployed one-by-one.

---

## 1. Executive Summary

We will treat an **agent as configuration**, not as a separately deployed application.

An agent is a versioned definition containing references to:

- a prompt;
- a model/provider;
- model settings;
- an allowed set of tools/capabilities;
- optional sub-agents/handoffs;
- output schema;
- guardrails;
- runtime limits.

The runtime reads the definition, resolves the prompt and model, binds only approved tools, constructs an OpenAI Agents SDK `Agent`, and executes it.

The architecture deliberately separates four concerns:

1. **Business domain** — owns business state, business rules, authorization, deterministic workflows and mandatory data retrieval.
2. **Agent control plane** — owns versioned agent definitions, publication status and deployment configuration.
3. **Agent runtime** — stateless execution layer that assembles and runs agents.
4. **AI observability/prompt management** — Langfuse stores/version-controls prompts and receives traces/evaluations.

The core design rule is:

> **Use deterministic business logic for steps we already know must happen; use the agent for judgment, interpretation, synthesis and adaptive orchestration.**

The agent runtime must **not become a second business-service layer**.

---

# 2. Target Architecture

```text
                               ┌──────────────────────────┐
                               │      Business Domain     │
                               │                          │
                               │ auth / permissions       │
                               │ business rules           │
                               │ deterministic workflow   │
                               │ persistence              │
                               │ mandatory data retrieval │
                               └────────────┬─────────────┘
                                            │
                                 invoke agent + context
                                            │
                                            ▼
┌──────────────────────┐         ┌──────────────────────────┐
│      PostgreSQL      │────────▶│   Stateless Agent Runtime│
│                      │         │                          │
│ agent definitions    │         │ config resolver          │
│ versions             │         │ agent factory            │
│ publication state    │         │ model registry           │
│ environment bindings │         │ capability registry      │
└──────────────────────┘         │ OpenAI Agents SDK        │
                                 └────────────┬─────────────┘
                                              │
                      ┌───────────────────────┼───────────────────────┐
                      │                       │                       │
                      ▼                       ▼                       ▼
                   OpenAI                   JAIS               Other models

                                              │
                    optional tool invocation │
                                              ▼
                                 ┌──────────────────────────┐
                                 │ Business APIs / MCP      │
                                 │                          │
                                 │ customer                 │
                                 │ property                 │
                                 │ case                     │
                                 │ finance                  │
                                 │ documents                │
                                 └──────────────────────────┘

                       Agent runtime + model/tool traces
                                      │
                                      ▼
                              ┌───────────────┐
                              │   Langfuse    │
                              │               │
                              │ prompts       │
                              │ versions      │
                              │ traces        │
                              │ evaluations   │
                              └───────────────┘
```

---

# 3. Design Principles

## 3.1 Agent runtime is stateless

The agent runtime must be horizontally scalable.

Do not depend on process-local memory for:

- user sessions;
- workflow state;
- business state;
- conversation ownership;
- agent definition state.

A runtime instance should be disposable.

Persistent state belongs in durable systems such as PostgreSQL, Redis, an event/workflow store, or a dedicated conversation store.

---

## 3.2 Business domain is the source of truth

The agent does not own:

- customers;
- applications;
- cases;
- contracts;
- payments;
- permissions;
- business rules;
- workflow status.

The domain services do.

Example:

```text
Business service:
1. authenticate caller
2. load customer
3. load application
4. calculate eligibility
5. construct AI context
6. invoke configured agent

Agent:
7. interpret facts
8. classify / explain / recommend / generate
```

Do **not** ask the model to call `customer.get` if the workflow already knows that customer data is always required.

---

## 3.3 Prefer bounded autonomy

Agents can operate in three modes.

### Mode A — Context-only

The business domain supplies everything required.

```text
Business service ──customer/application/rules──▶ Agent
                                               └─ reason/generate
```

Use this by default for predictable workflows.

### Mode B — Context + optional tools

The business domain supplies baseline context. The agent may retrieve additional information if its reasoning requires it.

```text
Business service
   │
   ├── customer
   ├── application
   └── decision
          │
          ▼
        Agent
          │
          ├── maybe policy.search
          ├── maybe case.history
          └── maybe property.lookup
```

This will likely be the most useful enterprise pattern.

### Mode C — Agentic investigation

The domain supplies a goal and boundaries and allows the agent to choose a sequence of tools.

Use only where the sequence is genuinely unknown in advance, e.g. research or investigation.

---

## 3.4 Configuration chooses capabilities; configuration does not implement capabilities

The database may say:

```json
{
  "tools": [
    "policy.search",
    "case.history"
  ]
}
```

It must **not** contain arbitrary executable Python, SQL, shell commands, or uncontrolled URLs.

Actual implementations are registered by trusted application code.

---

## 3.5 Tool interfaces live near the agent; business logic stays in the domain service

A tool in the runtime is normally a thin adapter:

```text
Agent
  │
  ▼
Tool schema / adapter
  │
  ▼
HTTP / gRPC / MCP
  │
  ▼
Business service
  │
  ├── authentication
  ├── authorization
  ├── validation
  ├── business rules
  └── persistence
```

Do not duplicate domain rules inside the agent runtime.

---

## 3.6 The agent is not an authorization boundary

Giving an agent the tool name `payment.refund` is not sufficient authorization.

A write/action tool must still be validated by the owning business service using the effective caller identity and tenant context.

Avoid an all-powerful `ai-admin` service identity.

---

## 3.7 Prefer schemas over prose contracts

Use typed request/response models for:

- agent invocation;
- context;
- tool inputs;
- tool outputs;
- structured agent outputs;
- configuration validation.

Use Pydantic in Python.

---

## 3.8 Version anything that can change behavior

At minimum record:

- agent definition version;
- resolved Langfuse prompt version;
- model provider;
- model name;
- model settings;
- enabled tool set;
- sub-agent versions;
- output schema version;
- application/runtime version.

Every production trace should allow us to reconstruct what actually ran.

---

# 4. Technology Choices

## 4.1 OpenAI Agents SDK — runtime/orchestration

Use the OpenAI Agents SDK as the execution engine.

Its useful primitives for this architecture are:

- `Agent`;
- `Runner`;
- function tools;
- agents-as-tools;
- handoffs;
- guardrails;
- MCP integration;
- per-agent model selection.

The SDK is the runtime, **not our persistent agent definition format**.

Our internal `AgentConfig` is the abstraction. This keeps the business application insulated from SDK-specific objects.

---

## 4.2 PostgreSQL + JSONB — agent registry

Use PostgreSQL as the system of record for agent definitions.

Use normal relational columns for operational metadata and `JSONB` for the evolving agent specification.

Why:

- versioning;
- transactional publication;
- auditability;
- querying;
- environment bindings;
- flexible schema evolution;
- no code deployment for configuration changes.

Do not normalize every configuration property into separate tables in the first prototype.

---

## 4.3 Langfuse — prompts + observability + evaluations

Use Langfuse for:

- prompt content;
- prompt versions;
- prompt labels such as `staging` / `production`;
- prompt experiments;
- model/agent traces;
- evaluation data.

Do not duplicate full prompt bodies inside the agent-registry database.

The agent config stores a prompt reference.

### Reproducibility rule

At runtime, always capture the exact **resolved prompt version** returned by Langfuse.

For a stronger production model:

- draft configuration may reference a label;
- publishing an immutable agent version resolves that label;
- the published definition pins the resulting prompt version.

This means `customer-advisor v12` always means the same executable configuration.

---

## 4.4 Existing business APIs first; MCP where justified

For the first prototype, tools should call existing internal HTTP/gRPC APIs.

Introduce MCP where a domain exposes a meaningful reusable AI-facing capability surface or where multiple AI runtimes need the same tool catalogue.

Do not adopt MCP merely to wrap one trivial endpoint.

---

# 5. Data Model

## 5.1 Minimum PostgreSQL schema

```sql
CREATE TABLE agent_definitions (
    id UUID PRIMARY KEY,
    agent_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('draft', 'published', 'archived')),
    config JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    UNIQUE (agent_key, version)
);

CREATE INDEX ix_agent_definitions_agent_key
    ON agent_definitions (agent_key);

CREATE INDEX ix_agent_definitions_config_gin
    ON agent_definitions USING GIN (config);


CREATE TABLE agent_environment_bindings (
    agent_key TEXT NOT NULL,
    environment TEXT NOT NULL
        CHECK (environment IN ('dev', 'test', 'staging', 'production')),
    agent_version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT NOT NULL,
    PRIMARY KEY (agent_key, environment),
    FOREIGN KEY (agent_key, agent_version)
        REFERENCES agent_definitions(agent_key, version)
);
```

### Why a separate environment binding?

Definitions stay immutable.

Promotion changes:

```text
customer-advisor / production
v11 → v12
```

rather than editing `v11`.

Rollback becomes moving the binding back to `v11`.

---

## 5.2 Example agent configuration

```json
{
  "schema_version": 1,
  "name": "Customer Advisor",
  "description": "Explains customer cases and recommends next actions.",

  "prompt": {
    "name": "agents/customer-advisor",
    "version": 42
  },

  "model": {
    "provider": "openai",
    "name": "gpt-5.6-sol",
    "settings": {
      "reasoning_effort": "medium"
    }
  },

  "tools": [
    "policy.search",
    "case.history"
  ],

  "sub_agents": [
    {
      "agent_key": "arabic-specialist",
      "environment": "production",
      "mode": "tool",
      "tool_name": "arabic_specialist",
      "description": "Use for specialist Arabic-language generation."
    }
  ],

  "runtime": {
    "max_turns": 8,
    "timeout_seconds": 45
  },

  "output": {
    "schema": "customer_advice_v1"
  },

  "guardrails": [
    "tenant_scope"
  ]
}
```

---

# 6. Configuration Schema

Use Pydantic to reject invalid configuration before it reaches the runtime.

```python
from typing import Any, Literal
from pydantic import BaseModel, Field


class PromptRef(BaseModel):
    name: str
    version: int | None = None
    label: str | None = None


class ModelRef(BaseModel):
    provider: str
    name: str
    settings: dict[str, Any] = Field(default_factory=dict)


class SubAgentRef(BaseModel):
    agent_key: str
    environment: str = "production"
    mode: Literal["tool", "handoff"]
    tool_name: str | None = None
    description: str | None = None


class RuntimeConfig(BaseModel):
    max_turns: int = Field(default=8, ge=1, le=50)
    timeout_seconds: int = Field(default=45, ge=1, le=300)


class OutputConfig(BaseModel):
    schema: str | None = None


class AgentConfig(BaseModel):
    schema_version: int = 1
    name: str
    description: str | None = None

    prompt: PromptRef
    model: ModelRef

    tools: list[str] = Field(default_factory=list)
    sub_agents: list[SubAgentRef] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
```

Validation must happen:

1. when saving a draft;
2. again when publishing;
3. defensively when loading into the runtime.

---

# 7. Prompt Management in Langfuse

## 7.1 Naming

Use stable hierarchical names:

```text
agents/customer-advisor
agents/arabic-specialist
agents/planning-advisor
agents/case-investigator
```

Keep the prompt name stable while Langfuse versions change.

---

## 7.2 Draft / staging / production workflow

Recommended workflow:

```text
Edit prompt
    │
    ▼
new Langfuse version
    │
    ▼
latest
    │
    ▼
eval suite
    │
    ▼
staging label
    │
    ▼
integration tests
    │
    ▼
production label
```

For immutable agent releases, resolve and pin the specific prompt version when the agent version is published.

---

## 7.3 Fetching a prompt

```python
from langfuse import get_client

langfuse = get_client()


def load_prompt(ref: PromptRef) -> tuple[str, int]:
    if ref.version is not None:
        prompt = langfuse.get_prompt(
            ref.name,
            version=ref.version,
        )
    else:
        prompt = langfuse.get_prompt(
            ref.name,
            label=ref.label or "production",
        )

    compiled = prompt.compile()
    resolved_version = prompt.version

    return compiled, resolved_version
```

Do not silently fall back from a missing production prompt to `latest`.

Fail closed.

---

# 8. Model Registry

The runtime should resolve model configuration through a controlled registry.

Do not let arbitrary database values create arbitrary network clients.

```python
from openai import AsyncOpenAI
from agents import OpenAIChatCompletionsModel


class ModelRegistry:
    def __init__(self, settings):
        self.settings = settings

        self.jais_client = AsyncOpenAI(
            api_key=settings.JAIS_API_KEY,
            base_url=settings.JAIS_BASE_URL,
        )

    def resolve(self, provider: str, model_name: str):
        if provider == "openai":
            return model_name

        if provider == "jais":
            # Assumes the JAIS serving layer exposes an
            # OpenAI-compatible Chat Completions endpoint.
            return OpenAIChatCompletionsModel(
                model=model_name,
                openai_client=self.jais_client,
            )

        raise ValueError(f"Unsupported model provider: {provider}")
```

The exact JAIS adapter depends on how JAIS is hosted.

The platform should make provider support explicit rather than pretending all providers have identical capabilities.

Example capability metadata:

```python
MODEL_CAPABILITIES = {
    "openai:gpt-5.6-sol": {
        "tool_calling": True,
        "structured_output": True,
        "multimodal": True,
    },
    "jais:your-model-name": {
        "tool_calling": False,       # set from actual deployment
        "structured_output": False,  # set from actual deployment
        "multimodal": False,
    },
}
```

Publishing should reject an agent configuration that requests capabilities its model does not support.

---

# 9. Capability / Tool Registry

## 9.1 Important distinction

A capability registry maps a trusted identifier to trusted implementation code.

```text
Config:
"policy.search"

            │ resolve

Runtime:
policy_search tool adapter

            │ call

Business API:
Policy Service
```

The database never supplies arbitrary implementation code.

---

## 9.2 Example thin tool adapter

```python
from agents import function_tool


class PolicyClient:
    async def search(
        self,
        *,
        query: str,
        tenant_id: str,
        access_token: str,
    ) -> dict:
        # Call internal Policy Service here.
        ...


def make_policy_search_tool(
    policy_client: PolicyClient,
    tenant_id: str,
    access_token: str,
):
    @function_tool
    async def policy_search(query: str) -> dict:
        """Search approved policies relevant to a question."""
        return await policy_client.search(
            query=query,
            tenant_id=tenant_id,
            access_token=access_token,
        )

    return policy_search
```

The business service remains responsible for actual authorization.

---

## 9.3 Capability registry

```python
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Capability:
    key: str
    risk: str
    factory: Callable


CAPABILITY_REGISTRY = {
    "policy.search": Capability(
        key="policy.search",
        risk="read",
        factory=make_policy_search_tool,
    ),
    "case.history": Capability(
        key="case.history",
        risk="read",
        factory=make_case_history_tool,
    ),
}
```

At runtime:

```python
def resolve_tools(tool_keys: list[str], runtime_context):
    tools = []

    for key in tool_keys:
        capability = CAPABILITY_REGISTRY.get(key)

        if capability is None:
            raise ValueError(f"Unknown capability: {key}")

        runtime_context.capability_policy.assert_allowed(key)

        tools.append(
            capability.factory(
                **runtime_context.dependencies_for(key)
            )
        )

    return tools
```

---

# 10. Read Tools vs Action Tools

Classify tool risk.

## Read tools

Examples:

```text
customer.lookup
property.search
document.search
case.history
policy.search
```

These still require tenant/user authorization, but usually do not require an explicit human approval step.

## Action tools

Examples:

```text
case.create
customer.update
email.send
payment.refund
contract.submit
```

For action tools:

1. validate the tool is allowed for this agent;
2. propagate the effective caller;
3. validate in the business service;
4. consider an approval step for high-impact actions;
5. make operations idempotent where possible;
6. audit the request and result.

Do not let the LLM itself decide whether it is authorized.

---

# 11. Runtime Context

The business service should invoke the runtime with explicit identity and request context.

Example request:

```json
{
  "agent_key": "customer-advisor",
  "environment": "production",
  "input": "Explain why this application was rejected.",
  "context": {
    "customer": {
      "id": "C123",
      "segment": "enterprise"
    },
    "application": {
      "id": "A992",
      "status": "rejected"
    },
    "decision": {
      "reason_codes": [
        "POLICY_17"
      ]
    }
  },
  "execution": {
    "tenant_id": "T001",
    "user_id": "U812",
    "correlation_id": "REQ-09F4"
  }
}
```

Do not send roles/permissions as user-editable prompt text and trust the model with enforcement.

Identity should be carried separately in trusted runtime context.

---

# 12. Agent Factory

The factory turns our internal configuration into SDK objects.

```python
from agents import Agent, ModelSettings


class AgentFactory:
    def __init__(
        self,
        *,
        agent_repository,
        prompt_service,
        model_registry,
        capability_registry,
        output_schema_registry,
        guardrail_registry,
    ):
        self.agent_repository = agent_repository
        self.prompt_service = prompt_service
        self.model_registry = model_registry
        self.capability_registry = capability_registry
        self.output_schema_registry = output_schema_registry
        self.guardrail_registry = guardrail_registry

    async def build(
        self,
        *,
        agent_key: str,
        environment: str,
        runtime_context,
        visited: set[str] | None = None,
    ) -> Agent:
        visited = visited or set()

        node_id = f"{agent_key}:{environment}"
        if node_id in visited:
            raise ValueError(
                f"Cyclic agent dependency detected at {node_id}"
            )

        visited.add(node_id)

        definition = await self.agent_repository.get_active(
            agent_key=agent_key,
            environment=environment,
        )

        config = AgentConfig.model_validate(definition.config)

        instructions, prompt_version = (
            await self.prompt_service.resolve(config.prompt)
        )

        runtime_context.trace_metadata["prompt_version"] = prompt_version
        runtime_context.trace_metadata["agent_version"] = definition.version

        model = self.model_registry.resolve(
            config.model.provider,
            config.model.name,
        )

        tools = self.capability_registry.resolve_many(
            config.tools,
            runtime_context,
        )

        handoffs = []

        for ref in config.sub_agents:
            sub_agent = await self.build(
                agent_key=ref.agent_key,
                environment=ref.environment,
                runtime_context=runtime_context,
                visited=set(visited),
            )

            if ref.mode == "tool":
                tools.append(
                    sub_agent.as_tool(
                        tool_name=ref.tool_name or ref.agent_key.replace("-", "_"),
                        tool_description=ref.description
                        or f"Use the {ref.agent_key} specialist.",
                    )
                )
            elif ref.mode == "handoff":
                handoffs.append(sub_agent)

        output_type = self.output_schema_registry.resolve(
            config.output.schema
        )

        guardrails = self.guardrail_registry.resolve_many(
            config.guardrails
        )

        model_settings = ModelSettings(
            **self._validated_model_settings(config.model.settings)
        )

        return Agent(
            name=config.name,
            instructions=instructions,
            model=model,
            model_settings=model_settings,
            tools=tools,
            handoffs=handoffs,
            output_type=output_type,
            input_guardrails=guardrails.input,
            output_guardrails=guardrails.output,
        )

    def _validated_model_settings(self, settings: dict) -> dict:
        allowed = {
            "temperature",
            "top_p",
        }
        return {
            k: v
            for k, v in settings.items()
            if k in allowed
        }
```

The prototype can initially omit sub-agents and guardrails, then add them after the basic path works.

---

# 13. Running the Agent

```python
from agents import Runner


class AgentRuntime:
    def __init__(self, factory):
        self.factory = factory

    async def run(
        self,
        *,
        agent_key: str,
        environment: str,
        user_input: str,
        business_context: dict,
        runtime_context,
    ):
        agent = await self.factory.build(
            agent_key=agent_key,
            environment=environment,
            runtime_context=runtime_context,
        )

        input_message = {
            "request": user_input,
            "business_context": business_context,
        }

        result = await Runner.run(
            agent,
            input=str(input_message),
            context=runtime_context,
            max_turns=runtime_context.max_turns,
        )

        return result.final_output
```

For the prototype, serializing the structured business context into the input is acceptable.

For production, define a consistent serialization strategy and avoid leaking internal fields the model does not need.

---

# 14. Business-Service Integration

## Recommended call pattern

Business orchestration:

```python
async def explain_application_rejection(
    *,
    application_id: str,
    caller,
):
    application = await application_repo.get(application_id)

    authorize_application_read(caller, application)

    customer = await customer_service.get(
        application.customer_id
    )

    decision = await decision_service.get(
        application.decision_id
    )

    context = {
        "customer": customer.to_ai_view(),
        "application": application.to_ai_view(),
        "decision": decision.to_ai_view(),
    }

    return await ai_runtime_client.run(
        agent_key="customer-advisor",
        input="Explain this rejection and propose next actions.",
        context=context,
        caller=caller,
    )
```

The agent does **not** need to rediscover the customer/application/decision through tools.

It receives the predictable baseline context.

---

# 15. API Contract for the Stateless Runtime

Example FastAPI endpoint:

```python
from typing import Any
from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI()


class ExecutionContext(BaseModel):
    tenant_id: str
    user_id: str
    correlation_id: str


class AgentRunRequest(BaseModel):
    agent_key: str
    environment: str = "production"
    input: str
    context: dict[str, Any] = Field(default_factory=dict)
    execution: ExecutionContext


class AgentRunResponse(BaseModel):
    output: Any
    agent_version: int
    prompt_version: int | None
    trace_id: str | None


@app.post("/v1/agents/run", response_model=AgentRunResponse)
async def run_agent(request: AgentRunRequest):
    runtime_context = build_runtime_context(request.execution)

    result = await agent_runtime.run(
        agent_key=request.agent_key,
        environment=request.environment,
        user_input=request.input,
        business_context=request.context,
        runtime_context=runtime_context,
    )

    return AgentRunResponse(
        output=result.output,
        agent_version=result.agent_version,
        prompt_version=result.prompt_version,
        trace_id=result.trace_id,
    )
```

In production, do **not** trust `tenant_id` / `user_id` merely because they arrive in JSON.

Derive or validate them from authenticated service/user credentials.

---

# 16. Observability with Langfuse

The runtime should capture enough metadata to answer:

- Which agent ran?
- Which agent version?
- Which prompt name/version?
- Which model/provider?
- Which tools were enabled?
- Which tools were called?
- Which tenant/use case?
- What latency/cost resulted?
- Which evaluation scores apply?

Minimum trace metadata:

```python
trace_metadata = {
    "agent_key": "customer-advisor",
    "agent_version": 12,
    "environment": "production",
    "prompt_name": "agents/customer-advisor",
    "prompt_version": 42,
    "model_provider": "openai",
    "model_name": "gpt-5.6-sol",
    "toolset": [
        "policy.search",
        "case.history",
    ],
    "tenant_id": "T001",
    "correlation_id": "REQ-09F4",
}
```

Do not attach secrets, access tokens or unnecessarily sensitive business payloads to traces.

Use Langfuse/OpenTelemetry integration for OpenAI Agents SDK tracing rather than building bespoke tracing around every model call.

---

# 17. Caching

The stateless runtime may still use caches.

Recommended cache keys:

```text
agent definition:
agent:{agent_key}:{version}

prompt:
handled primarily by Langfuse SDK prompt cache

assembled agent:
agent-object:{agent_key}:{version}:{runtime-build-version}
```

Do not cache per-user authorization decisions across users.

Be careful caching assembled agents if tool objects capture request-specific:

- access tokens;
- tenant IDs;
- user IDs;
- request context.

A safe approach is:

1. cache immutable agent configuration;
2. cache prompt/model metadata;
3. build request-bound tool adapters per invocation.

---

# 18. Publishing and Rollback

Recommended state machine:

```text
draft
  │
  ├── schema validation
  ├── capability validation
  ├── model-capability validation
  ├── prompt resolution
  ├── automated eval
  └── integration test
  │
  ▼
published
```

Publishing creates or locks an immutable version.

Environment binding selects which published version is active.

```text
customer-advisor

v11 ────────────── archived/available
v12 ────────────── published
v13 ────────────── draft

production ───────▶ v12
staging ──────────▶ v13 (after publishing)
```

Rollback changes the environment binding; no agent runtime deployment is required.

---

# 19. Suggested Repository Layout

Keep the runtime and business service separately deployable, even if they initially live in the same monorepo.

```text
repo/
├── services/
│   ├── business-api/
│   │   ├── domain/
│   │   ├── application/
│   │   ├── api/
│   │   └── clients/
│   │
│   └── agent-runtime/
│       ├── api/
│       ├── config/
│       ├── registry/
│       │   ├── models.py
│       │   ├── capabilities.py
│       │   ├── outputs.py
│       │   └── guardrails.py
│       ├── runtime/
│       │   ├── factory.py
│       │   ├── runner.py
│       │   └── context.py
│       ├── integrations/
│       │   ├── langfuse.py
│       │   ├── openai.py
│       │   ├── jais.py
│       │   └── business_clients/
│       └── main.py
│
├── packages/
│   └── contracts/
│       ├── ai_api.py
│       └── domain_dtos.py
│
├── migrations/
└── docker-compose.yml
```

Shared packages may contain DTOs/contracts.

Avoid sharing repositories/domain implementations into the agent runtime.

---

# 20. Prototype Scope

The first prototype should prove the architecture, not implement every future feature.

## Build in prototype 1

- PostgreSQL `agent_definitions`;
- PostgreSQL `agent_environment_bindings`;
- Pydantic `AgentConfig`;
- Langfuse prompt retrieval;
- OpenAI model registry;
- optional JAIS model adapter if an endpoint is already available;
- two read-only capabilities;
- agent factory;
- `/v1/agents/run`;
- OpenAI Agents SDK execution;
- Langfuse tracing;
- immutable version + environment promotion;
- one business-service caller that preloads mandatory context.

## Defer

- visual agent builder;
- arbitrary dynamic MCP discovery;
- complex human approval workflows;
- dozens of model providers;
- automated prompt optimization;
- complicated multi-agent graphs;
- per-tenant agent customization;
- dynamic code/plugin upload.

---

# 21. One-Go Prototype Scenario

Implement this exact scenario first.

## Agent

`customer-advisor`

## Business flow

Request:

```text
Explain why application A992 was rejected and what the customer
can do next.
```

Business service deterministically retrieves:

```text
customer
application
decision
```

The agent receives those as context.

The agent has two optional tools:

```text
policy.search
case.history
```

The agent may use them only if baseline context is insufficient.

Prompt lives in Langfuse:

```text
agents/customer-advisor
```

Postgres agent configuration:

```json
{
  "schema_version": 1,
  "name": "Customer Advisor",

  "prompt": {
    "name": "agents/customer-advisor",
    "label": "production"
  },

  "model": {
    "provider": "openai",
    "name": "gpt-5.6-sol",
    "settings": {}
  },

  "tools": [
    "policy.search",
    "case.history"
  ],

  "sub_agents": [],

  "runtime": {
    "max_turns": 6,
    "timeout_seconds": 30
  },

  "output": {
    "schema": "customer_advice_v1"
  },

  "guardrails": []
}
```

Expected structured output:

```python
from pydantic import BaseModel


class CustomerAdvice(BaseModel):
    explanation: str
    reason_codes: list[str]
    next_actions: list[str]
    additional_information_used: list[str]
```

---

# 22. Prototype Acceptance Criteria

The prototype is complete when engineers can demonstrate all of the following:

1. Create `customer-advisor v1` in PostgreSQL.
2. Create/version its prompt in Langfuse.
3. Bind `customer-advisor / production` to v1.
4. Call the runtime from the business service with preloaded context.
5. Runtime loads the active definition.
6. Runtime resolves the Langfuse prompt.
7. Runtime resolves the configured model.
8. Runtime binds only approved tools.
9. Runtime builds an SDK `Agent`.
10. `Runner` executes the request.
11. Optional tool calls reach business-service APIs rather than local domain logic.
12. Langfuse contains the trace with agent/prompt/model metadata.
13. Create v2 without modifying v1.
14. Promote production from v1 to v2 without deploying the container.
15. Roll back from v2 to v1 without deploying the container.
16. Attempting to configure an unknown tool fails validation.
17. Attempting to use a capability not allowed by policy fails.
18. A domain write operation cannot bypass domain authorization.

---

# 23. Recommended Operational Rules

## MUST

- Keep production agent versions immutable.
- Persist the exact resolved prompt version with each trace.
- Validate configuration before publication.
- Keep business rules in business/domain services.
- Propagate trusted caller/tenant context.
- Re-authorize action tools in the owning domain service.
- Use allow-listed model providers and capabilities.
- Treat model/provider feature support explicitly.
- Keep secrets out of agent configuration.
- Make tool/action calls auditable.
- Set runtime turn/time limits.
- Add cycle detection for agent dependencies.

## SHOULD

- Prefer context-first execution over tool-first execution.
- Prefer read tools before action tools.
- Use structured outputs where supported.
- Use existing HTTP/gRPC APIs before introducing MCP everywhere.
- Add MCP when capabilities are broadly reusable.
- Keep agent and business services separately deployable.
- Keep them in the same monorepo initially unless ownership requires otherwise.
- Cache immutable configuration but bind request-specific security context per call.

## MUST NOT

- Store executable Python/SQL/shell code in agent configuration.
- Allow arbitrary tool endpoints from DB configuration.
- Put core business authorization in prompts.
- Give the agent direct database access simply for convenience.
- Duplicate domain rules inside tool wrappers.
- Treat an LLM recommendation as final authorization.
- Trust tenant/user IDs supplied by an unauthenticated caller.
- Mutate a published definition in place.
- Depend on the runtime container's local memory for durable state.

---

# 24. Decision Summary

| Area | Decision |
|---|---|
| Runtime | Stateless OpenAI Agents SDK service |
| Repository | Monorepo initially; separate deployable container |
| Agent definitions | PostgreSQL |
| Flexible config format | `JSONB` |
| Config validation | Pydantic |
| Agent versioning | Immutable version rows |
| Environment promotion | Separate environment-binding table |
| Prompt storage | Langfuse |
| Prompt versioning | Langfuse versions/labels; record resolved version |
| Production prompt reproducibility | Prefer pinning exact prompt version at publish |
| Observability | Langfuse + OpenTelemetry integration |
| Business state | Domain services/databases |
| Mandatory data retrieval | Business service |
| Adaptive/optional retrieval | Agent tools |
| Tool implementation | Thin runtime adapter → business API/MCP |
| Authorization | Owning business/domain service |
| Model selection | Runtime model registry |
| Multi-provider | Per-agent/provider mapping; e.g. OpenAI + JAIS |
| Multi-agent | Configurable agents-as-tools/handoffs where justified |
| MCP | Adopt selectively for reusable capability surfaces |
| Agent runtime scaling | Horizontal/stateless |
| Production rollback | Change environment binding, no runtime deployment |

---

# 25. Mental Model for the Team

Do not think:

```text
"We are building many AI applications."
```

Think:

```text
"We are building one governed agent execution platform
that can execute many versioned agent definitions."
```

And do not think:

```text
"The agent owns the workflow."
```

Think:

```text
Business domain:
    knows what MUST happen.

Agent:
    decides what MAY be useful where judgment is required.
```

The desired result is intentionally boring infrastructure:

```text
Agent definition
      ↓
validate
      ↓
resolve prompt
      ↓
resolve model
      ↓
bind approved capabilities
      ↓
construct Agent
      ↓
Runner.run()
      ↓
trace
      ↓
return typed result
```

That is the foundation on which richer agentic behavior can be added safely later.

---

# 26. Implementation Checklist

```text
[ ] Create PostgreSQL migrations
[ ] Implement AgentConfig Pydantic models
[ ] Implement AgentRepository
[ ] Implement environment binding lookup
[ ] Configure Langfuse prompt management
[ ] Implement PromptService
[ ] Implement ModelRegistry
[ ] Implement CapabilityRegistry
[ ] Add policy.search adapter
[ ] Add case.history adapter
[ ] Implement OutputSchemaRegistry
[ ] Implement AgentFactory
[ ] Implement runtime request context
[ ] Add cycle detection
[ ] Implement AgentRuntime
[ ] Add FastAPI /v1/agents/run
[ ] Propagate authenticated tenant/user identity
[ ] Connect OpenAI Agents SDK
[ ] Connect Langfuse/OpenTelemetry tracing
[ ] Create customer-advisor prompt
[ ] Insert customer-advisor v1
[ ] Run end-to-end call
[ ] Verify optional tool invocation
[ ] Verify trace metadata
[ ] Publish v2
[ ] Promote v2 without container deployment
[ ] Roll back to v1
[ ] Add config validation tests
[ ] Add authorization tests
[ ] Add model capability compatibility tests
```

---

# 27. Implementation Notes Against Current SDKs

As of September 2026:

- OpenAI Agents SDK agents support instructions, tools, handoffs, guardrails, structured output and per-agent model configuration.
- The SDK uses the Responses API by default for OpenAI models.
- Non-OpenAI providers can be integrated per agent; OpenAI-compatible providers can use an `AsyncOpenAI` client with a custom base URL and `OpenAIChatCompletionsModel`.
- Providers differ in tool calling, structured output and multimodal support; validate capabilities rather than assuming parity.
- The Agents SDK supports MCP server integration.
- Langfuse prompt management provides immutable prompt versions and labels such as `production`, `staging` and `latest`.
- Langfuse can fetch prompts by exact version or label and client-side caching is available.
- Langfuse provides an OpenTelemetry-based integration for tracing OpenAI Agents SDK activity.

Engineers should verify installed package versions when implementing because exact SDK signatures can evolve.

---

## End State

The prototype should leave us with the following production-shaped boundary:

```text
                        Agent Control Plane
                 ┌────────────────────────────┐
                 │ PostgreSQL agent registry  │
                 │ Langfuse prompt versions   │
                 │ evaluation / promotion     │
                 └──────────────┬─────────────┘
                                │
                                ▼
                     Stateless Agent Runtime
                 ┌────────────────────────────┐
                 │ AgentFactory               │
                 │ ModelRegistry              │
                 │ CapabilityRegistry         │
                 │ OpenAI Agents SDK          │
                 │ Langfuse tracing           │
                 └──────────────┬─────────────┘
                                │
               ┌────────────────┼────────────────┐
               ▼                ▼                ▼
             OpenAI            JAIS          Other LLMs
                                │
                      optional capabilities
                                │
                                ▼
                     Business APIs / MCP
                                │
                                ▼
                         Business Domain
```

This architecture lets agent behavior evolve by configuration while keeping the durable business system deterministic, governed and independently testable.
