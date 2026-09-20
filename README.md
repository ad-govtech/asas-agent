# asas-agent

Agents as configuration. An agent is a versioned row in Postgres: a prompt reference, a model, the tools it may use, and the limits it runs under. This package resolves that definition and runs it on the OpenAI Agents SDK, with file prompts by default and optional Langfuse prompts and tracing.

Any product installs it, runs one migration, registers its own tools, and has agents running. No new service to write, and no redeploy to change an agent.

```
your service ──► asas-agent runtime ──► OpenAI / gateway models
      │                  │
      │                  ├── Postgres: which agent version is live
      │                  ├── Files / optional Langfuse: prompts
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
| Prompts | Files snapshotted in Postgres on publish, or Langfuse version references |
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
    inputs={"application": application.to_ai_view()},
    message="Explain this rejection and what the customer can do next.",
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

Publishing freezes the prompt: file prompts are stored in the agent definition as they are written, placeholders included; Langfuse labels such as `production` are resolved to an exact version. Editing a file does not change published agents. Create and publish a new agent version to adopt the edit. File snapshots travel with promotion and rollback, and runtime servers do not need the original files.

## Prompts, inputs and messages

A prompt is one block of text, or a chat prompt: **one system message, optionally followed by one user message**.

```json
[
  {"role": "system", "content": "You screen applications for {{entity}}."},
  {"role": "user", "content": "Application: {{application}}"}
]
```

The system message becomes the agent's instructions. The user message, if there is one, opens the run, so the prompt author decides where each fact lands. Any other shape is refused when the agent is published.

A run is given two things, and neither depends on the other:

- **`inputs`** fill the prompt's `{{placeholders}}`;
- **`message`** is what the caller is saying to the agent, and follows the prompt's own user message.

```python
result = await platform.runtime.run(
    agent_key="screening-assistant",
    environment="production",
    inputs={"application": application.to_ai_view()},
    run_name=f"screening-assistant-{application.id}",
    context=RuntimeContext(tenant_id="T001", user_id="U812", correlation_id="REQ-09F4"),
)
```

The same two fields exist on `POST /v1/agents/run`; the CLI takes `--input name=value` and an optional message.

**What may be filled is derived, never configured.** It is what the prompt asks for, minus what the definition answers in `prompt.variables`. A value the prompt does not use is refused, because it would never reach the model; a placeholder with no value is refused too, so the model is never handed a literal `{{application}}`. A request may add values but never replace one the definition sets: those reach the system instructions, and a caller able to rewrite one could rewrite a published agent's instructions.

Only the prompt's own placeholders count, so a CV or job description containing `{{...}}` is passed through as the data it is. Values render as Langfuse renders them - `str(value)`, and nothing at all for `None` - so a file prompt and a Langfuse prompt with the same text produce the same call. Their total size is capped by `ASAS_INPUTS_MAX_BYTES` (256 KB).

A sub-agent is filled by its own definition: nothing is forwarded from its parent, because the caller addressed the parent and cannot know what a specialist needs.

## Naming a run

One agent often runs many times in the same second - once per rubric area, once per candidate - and afterwards someone has to tell those runs apart: a person reading traces, or an evaluation harness that groups generations by name.

`run_name` is that name. It reaches the Langfuse observation and the trace it opens; the agent stays a tag and the correlation id is the session, so a fan-out is still findable as one agent and one request. Without it a run is `agent:<agent key>`. A name is plain text, at most 200 bytes, and cannot start with `agent:`, which is how the runtime names a run of an agent.

What the runtime records about a run - agent key and version, prompt name and version, which inputs were filled, model, toolset, tenant - is its own. A request cannot add to it or overwrite it: a trace is evidence of what ran.

## What runs share

A fan-out - one call per rubric area, one per candidate in a batch - starts dozens of runs in the same second, and each one has to know which version its environment is bound to. That question is answered from memory:

- Runs that ask **at the same moment share one query**: forty concurrent runs ask the registry once, not forty times. Measured against a local Postgres, that is 128 ms of queueing against a five-connection pool reduced to 5 ms.
- **Nothing is kept afterwards.** The next run asks again, so a promotion is visible without anything to configure or expire.
- A run that hits its deadline and walks away **leaves the answer for the others**; it does not cancel the query they are waiting on.
- A promotion made **through this process** detaches the query it affects, so a run arriving after it asks again. A promotion made elsewhere - another process, the CLI - cannot reach into this one, so a run arriving while a query is already open may still be given the version that was live when that query started. The window is one query; the run after it is current.
- Each run is handed **its own copy** of the definition, so one run cannot change what another reads.

Prompts are cached: Langfuse's SDK keeps them for 60 seconds, and a prompt file is re-read once it changes on disk. A replacement that preserves the file's timestamp and size (`cp -p`, `rsync -a`, a restored backup) looks unchanged, so restart after one.

## Rules the package enforces

- Configuration names capabilities; it never carries code, SQL, URLs or secrets.
- A tool that changes data is refused unless the caller enables action tools, and the owning service still authorizes the call.
- Identity travels in the runtime context, never in prompt text.
- A model that cannot call tools or return structured output is refused at publish time, not mid-conversation.
- Sub-agents that reference each other in a loop are refused.
- A missing production prompt is an error. The runtime never falls back to a draft.
- A prompt variable with no value is an error, not a placeholder left in the text.
- A request cannot replace a variable the published definition sets.
- A prompt that is not one system message, optionally followed by one user message, is refused when it is published, not when it runs.
- A message whose content is not text is refused.

## Choose prompts and tracing

The default installation needs **no Langfuse SDK, account, or server**. PostgreSQL is still required for the agent registry.

| Choice | Prompt storage | Additional service |
|---|---|---|
| `file` (default) | Markdown files when publishing; immutable snapshots in the registry at runtime | None |
| `langfuse` | Versioned prompts in your self-hosted Langfuse instance or Langfuse Cloud | Langfuse |

For file prompts:

```dotenv
ASAS_PROMPT_PROVIDER=file
ASAS_PROMPT_DIR=prompts
ASAS_TRACING_PROVIDER=none
```

A prompt named `agents/customer-advisor` reads `prompts/agents/customer-advisor.md` as a text prompt, or `prompts/agents/customer-advisor.chat.json` as a chat prompt. File prompts do not support numeric versions or custom labels; the agent version identifies the stored snapshot (`prompt_version` is null).

For Langfuse, install the optional dependency:

```bash
uv pip install "asas-agent[langfuse]"
```

Both **self-hosted and cloud deployments** use the same integration. For a government/internal deployment, set the URL and project keys from your own instance:

```dotenv
ASAS_PROMPT_PROVIDER=langfuse
LANGFUSE_HOST=https://langfuse.internal.example
LANGFUSE_PUBLIC_KEY=<your-instance-public-key>
LANGFUSE_SECRET_KEY=<your-instance-secret-key>
ASAS_TRACING_PROVIDER=none
```

The application must be able to reach that URL. For Langfuse Cloud, use `https://cloud.langfuse.com` (or your region's endpoint) and that project's keys.

Tracing is independent: set `ASAS_TRACING_PROVIDER=langfuse` to send traces to the configured instance, including when prompts use files. `none` disables runtime tracing, including the Agents SDK's built-in trace export. Choosing an internal backend never leaves an external one installed: if the span instrumentation is missing, or refuses to attach to the installed SDK version, the SDK's own exporter is removed rather than left pointing at OpenAI. `ASAS_TRACING=false` overrides the provider and disables tracing. There is no silent fallback when an explicitly selected provider is unavailable.

**Existing deployments:** set `ASAS_PROMPT_PROVIDER=langfuse` explicitly and install the extra to retain Langfuse prompts. Set `ASAS_TRACING_PROVIDER=langfuse` to retain tracing. Previously published file definitions are not rewritten: they continue reading files until replaced by a newly published version. Switching providers does not convert existing Langfuse version references; create new drafts referencing files to migrate those agents.

## Settings

| Variable | Purpose |
|---|---|
| `ASAS_REGISTRATION_MODULES` | Comma-separated Python modules registering application tools, schemas, and guardrails before startup/publication |
| `DATABASE_URL` | Postgres for the registry. `postgres://` and `?sslmode=require` are accepted |
| `ASAS_API_KEY` | Required as `X-API-Key` on the runtime API when set |
| `ASAS_PROMPT_PROVIDER` | `file` (default) or `langfuse` |
| `ASAS_PROMPT_DIR` | Prompt folder for file drafts (default: `prompts`) |
| `ASAS_TRACING_PROVIDER` | `none` (default) or `langfuse`, independent of prompts |
| `ASAS_INPUTS_MAX_BYTES` | Ceiling on the inputs one run may send (default: 256000) |
| `ASAS_TRACING` | Set `false` to disable tracing regardless of provider |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Prompts and traces |
| `OPENAI_API_KEY` | OpenAI models |
| `MODEL_GATEWAY_URL`, `MODEL_GATEWAY_KEY` | Any OpenAI-compatible gateway, such as the AI Factory model gateway |

Install the `tracing` extra for span-level Langfuse traces of every model and tool call:

```bash
uv add "asas-agent[tracing]"
```

## The thinking behind it

- [The engineering guideline](docs/agent-as-configuration-guideline.md) this package was built to: the architecture, the design principles, the data model and the rules a governed agent platform follows.
- [Design notes](docs/design.md): what this package implements, what it leaves to the calling application, and what is still open.
- [Recruiting console](examples/recruiting/README.md): a working app that shows versions, tools and rollback in a browser.

## Publication and execution checks

Publishing validates model capabilities/settings, registered tool/schema/guardrail names, and the sub-agent graph without executing tools or calling models. Promotion rechecks the graph because environment bindings may have changed. Register application components before calling `build_platform()`. For CLI usage, set `ASAS_REGISTRATION_MODULES`, for example `examples.recruiting.capabilities,examples.recruiting.schemas`. These are trusted application modules, never code from an agent definition.

New model names require explicit capability metadata before publication and runtime use:

```python
from asas_agent.integrations.models import ModelCapabilities

platform.models.capabilities["gateway:my-model"] = ModelCapabilities(
    tool_calling=True, structured_output=True,
)
```

`reasoning_effort` is translated to the SDK's `reasoning.effort`; unsupported settings are rejected. Model clients use the credentials from `Settings`, including `.env` values. The OpenAI client is constrained to the tested 2.29 series for compatibility with Agents SDK 0.8.

Every runtime entry point applies the minimum of caller, definition, and platform limits. Deadlines include agent assembly and cancel pending async work. Tool sub-agents have their own bounded runs within the parent's deadline. Handoffs share one run and use the strictest limits in the handoff chain. `build_platform(dependencies=...)` supplies defaults to direct calls as well as HTTP requests; request dependencies override defaults without mutating them. HTTP deadline failures return 504 and exhausted turn budgets return 422.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
uv run ruff check .
node --test tests/recruiting-security.test.mjs
```

The suite includes real SDK execution with a local model and mocked HTTP transport; it needs no model credentials. PostgreSQL concurrency and publication tests are opt-in locally and run in CI:

```bash
ASAS_TEST_DATABASE_URL=postgresql+asyncpg://asas:asas@localhost:5433/asas_agent uv run pytest tests/test_postgres.py
```

These database tests create and remove uniquely named schemas. Use a disposable development database. CI runs the Python suite, PostgreSQL tests, console security tests, and package build on Python 3.11 and 3.12.

Part of the Abu Dhabi Government AI Factory Commons. Built by XD.AI.
