# asas-agent

Agents as configuration. An agent is a versioned row in Postgres: a prompt reference, a model, the tools it may use, and the limits it runs under. This package resolves that definition and runs it on the OpenAI Agents SDK, with prompts and traces in Langfuse.

Any product installs it, runs one migration, registers its own tools, and has agents running. No new service to write, and no redeploy to change an agent.

```
your service ──► asas-agent runtime ──► OpenAI / gateway models
      │                  │
      │                  ├── Postgres: which agent version is live
      │                  ├── Langfuse: prompt text and versions
      │                  └── your tools: thin adapters to your APIs
      └── loads what it already knows the agent needs
```

## Ten minutes

```bash
uv add asas-agent                       # or: pip install asas-agent
docker compose up -d postgres           # or point at any Postgres

asas-agent init --database-url postgresql://asas:asas@localhost:5433/asas_agent
asas-agent migrate                      # creates the registry tables
asas-agent agent add customer-advisor --publish --promote dev
asas-agent run customer-advisor "Explain this rejection." --env dev
asas-agent serve                        # POST /v1/agents/run on :8080
```

`asas-agent doctor` checks the database, prompts, model keys and registered tools, and says what is missing.

## What each piece is for

| Piece | What it does |
|---|---|
| `agent_definitions` | Every version of every agent, as JSONB. Published versions never change |
| `agent_environment_bindings` | Which version each environment runs. Promotion and rollback move this row |
| Langfuse | Prompt text and versions. The runtime records the version it used |
| Capability registry | Names in configuration bound to code in your app. The database never carries an implementation |
| Output schemas | A Pydantic model per name, so an agent can return typed results |
| Runtime API | `POST /v1/agents/run`, stateless, scale it horizontally |

## Using it from your service

```python
from asas_agent import RuntimeContext, build_platform, capability, output_schema
from pydantic import BaseModel


@output_schema("customer_advice_v1")
class CustomerAdvice(BaseModel):
    explanation: str
    next_actions: list[str]


@capability("policy.search", risk="read")
def make_policy_search(context: RuntimeContext):
    from agents import function_tool

    @function_tool
    async def policy_search(query: str) -> list[dict]:
        """Find published policy clauses relevant to a question."""
        return await policy_client.search(query, token=context.access_token)

    return policy_search


platform = build_platform()
result = await platform.runtime.run(
    agent_key="customer-advisor",
    environment="production",
    user_input="Explain this rejection and what the customer can do next.",
    business_context={"application": application.to_ai_view()},
    context=RuntimeContext(tenant_id="T001", user_id="U812", correlation_id="REQ-09F4"),
)
```

Your service loads what it already knows must be loaded. The agent is for judgment, not for rediscovering your data.

## Versions, promotion, rollback

```bash
asas-agent agent add customer-advisor -f agents/customer-advisor.json   # v2 as a draft
asas-agent agent publish customer-advisor 2                             # locks v2, pins the prompt version
asas-agent agent promote customer-advisor 2 --env production            # production runs v2
asas-agent agent promote customer-advisor 1 --env production            # rolled back, no deployment
asas-agent agent list customer-advisor
```

Publishing resolves a prompt label such as `production` to the exact version live at that moment and stores it, so `customer-advisor v2` always means the same thing.

## Rules the package enforces

- Configuration names capabilities; it never carries code, SQL, URLs or secrets.
- A tool that changes data is refused unless the caller enables action tools, and the owning service still authorizes the call.
- Identity travels in the runtime context, never in prompt text.
- A model that cannot call tools or return structured output is refused at publish time, not mid-conversation.
- Sub-agents that reference each other in a loop are refused.
- A missing production prompt is an error. The runtime never falls back to a draft.

## Settings

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres for the registry. `postgres://` and `?sslmode=require` are accepted |
| `ASAS_API_KEY` | Required as `X-API-Key` on the runtime API when set |
| `ASAS_PROMPT_PROVIDER` | `langfuse` everywhere shared, `file` for local work |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Prompts and traces |
| `OPENAI_API_KEY` | OpenAI models |
| `MODEL_GATEWAY_URL`, `MODEL_GATEWAY_KEY` | Any OpenAI-compatible gateway, such as the AI Factory model gateway |

Install the `tracing` extra for span-level Langfuse traces of every model and tool call:

```bash
uv add "asas-agent[tracing]"
```

## Development

```bash
uv venv && uv pip install -e ".[dev,tracing]"
uv run pytest
uv run ruff check .
```

Part of the Abu Dhabi Government AI Factory Commons. Built by XD.AI.
