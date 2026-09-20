# Design notes

Built to the [Agent-as-Configuration Platform engineering guideline](agent-as-configuration-guideline.md) (September 2026). This file records what the package implements, what it deliberately leaves to the calling application, and what is still open.

## The boundary

```
Business service            asas-agent                        Outside
─────────────────           ──────────────────────            ──────────────
authenticates the caller    resolves the definition           Postgres registry
authorizes the action       resolves the prompt               Files / Langfuse prompts
loads mandatory data  ────► binds approved tools      ──────► OpenAI / gateway
applies business rules      builds the SDK agent              your business APIs
persists the outcome        runs it, records the trace        Optional Langfuse traces
```

The runtime holds no business state and no session. Anything durable lives in Postgres or in the calling service.

## What the package decides

| Decision | Choice | Why |
|---|---|---|
| Definition store | Postgres, JSONB `config` column | Versioning, transactional publishing, auditability, no deploy to change an agent |
| Version model | Immutable rows, one binding per environment | Promotion and rollback move a pointer; a published version never changes |
| Validation | Pydantic, on save, on publish, and again on load | Bad configuration never reaches a model call |
| Prompts | File drafts with registry snapshots, or optional Langfuse | Developers choose whether to operate a separate prompt service |
| Prompt pinning | Publishing stores the file template or pins a Langfuse version | `customer-advisor v2` always means the same thing |
| Prompt shape | Text or chat; system messages instruct, the rest open the run | The prompt author places each fact, instead of the runtime handing the model one JSON blob |
| Prompt variables | The definition's values publish with the agent; the request's arrive per call | A request's data cannot be frozen into a version; substitution happens once |
| Caching | Definitions by agent and environment, for a few seconds, with concurrent runs sharing one query | A fan-out asks the registry once; a rollback still lands without a deployment |
| Tools | Named in configuration, implemented in the app | The database never carries code, URLs or credentials |
| Action tools | Refused unless the caller enables them | An LLM naming a tool is not authorization |
| Models | Registry of providers, with a capability table | A model that cannot call tools is refused at publish, not mid-run |
| Execution | OpenAI Agents SDK | The SDK is the engine; `AgentConfig` is our own format |
| Tracing | Disabled by default; optional Langfuse configured separately from prompts | No observability service is required to run an agent |
| Trace naming | Per run, chosen by the caller, defaulting to the agent | One agent runs many times per request; a trace has to say which run it was |

## What the application owns

- **Capabilities.** Register a factory per tool name. The factory receives the request context and returns a thin adapter that calls your API with the caller's token.
- **Output schemas.** Register a Pydantic model per name; the runtime passes it to the SDK as the output type.
- **Guardrails.** Register input and output guardrails by name.
- **Context.** Load what you already know the agent needs, and pass it in. The agent should not rediscover your data through tools.
- **Authorization.** Always, in the owning service. The runtime propagates identity; it does not decide.

## Deliberately not in this version

- A visual agent builder. The CLI and the JSON definition are the interface for now.
- Dynamic MCP discovery. MCP servers can be added as capabilities; discovering them from configuration is not supported.
- Human approval workflows. `risk="action"` marks the tools that need one; the workflow belongs to the business service.
- Per-tenant agent variants. One definition per environment.
- Conversation storage. The runtime is stateless; a conversation store is the caller's.

## Open questions for the AI Factory

1. **Gateway contract.** The model registry assumes an OpenAI-compatible endpoint. Confirm the AI Factory model gateway's base URL, auth header and model naming, then the capability table can be filled from what it actually serves.
2. **Identity.** The runtime API trusts the tenant and user it is given, because it expects to sit behind a service-to-service boundary. If it is ever exposed more widely, it needs to verify a token instead.
3. **Evaluation gate.** The guideline puts an automated evaluation between draft and published. The CLI has the hook (`publish`), but no evaluation runner yet.
4. **Registry ownership.** One registry per product, or one shared registry for the AI Factory with an entity column. This version assumes one per product.

## Optional Langfuse

The original engineering guideline uses Langfuse as its reference deployment. This implementation also supports file prompts in shared environments: publication stores the prompt template, placeholders included, in the existing agent-definition JSONB, so no database migration or extra service is needed. The agent version identifies the snapshot. All publication paths use the repository's pinning logic; missing prompts prevent publication.

Langfuse remains available through the `langfuse` extra (or `tracing` for span instrumentation). `LANGFUSE_HOST` selects a self-hosted/internal instance or Cloud, using that instance's project keys. `ASAS_TRACING_PROVIDER` chooses `none` or `langfuse` independently of `ASAS_PROMPT_PROVIDER`. See the README for deployment and migration settings.

## Upgrading a registry written by a pre-release build

A prompt snapshot stores the template as written, placeholders included, and the definition's variables beside it. A pre-release build stored *rendered* text instead and dropped the variables, and the two are indistinguishable in the row.

This package has not been released, so no such rows are expected to exist. If a registry was populated by an earlier build, republish those agents before upgrading: a stored `Explain {{customer}}` that was already rendered would otherwise be read as a template and either demand a value for `customer` or substitute request data into text that used to be literal. A published version is immutable, so the fix is a new version, not an edit.

## Runtime and publication safeguards

The runtime owns dependency merging, platform ceilings, and cancellation deadlines for every entry point. Tool sub-agents get individual turn/deadline limits; handoffs share the strictest chain limits because the SDK executes them in one run. Langfuse retrieval and flush calls run outside the event loop. Cancelled runs leave buffered spans to the SDK exporter instead of extending their deadline to flush.

Publication checks references and capabilities without invoking tool factories. Promotion revalidates the current graph. PostgreSQL transaction-scoped advisory locks serialize version allocation per agent, including the first draft; a registry-wide binding lock serializes graph changes to prevent concurrent promotions creating cycles. Neither operation changes the database schema.
