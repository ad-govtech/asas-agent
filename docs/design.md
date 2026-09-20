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
| Prompt shape | Text, or one system message and at most one user message | The prompt author places each fact, and the shape is small enough to hold in your head |
| Prompt variables | The definition's values publish with the agent; the request's arrive per call, and are what the prompt asks for | A request's data cannot be frozen into a version, and what it may fill needs no configuration |
| Shared lookups | Runs asking at the same moment share one query; nothing is kept afterwards | A fan-out asks the registry once, and a promotion is visible to the next run |
| Tools | Named in configuration, implemented in the app | The database never carries code, URLs or credentials |
| Delegation | Not supported | No product asked for it, and it brought a graph, cycle checks, per-child budgets and a lock on every promotion |
| HTTP runtime | An optional extra | An application that embeds the runtime does not need a web framework |
| Action tools | Refused unless the caller enables them | An LLM naming a tool is not authorization |
| Models | Registry of providers | What a model can do is the provider's business, and it says so itself |
| Execution | OpenAI Agents SDK | The SDK is the engine; `AgentConfig` is our own format |
| Tracing | Disabled by default; optional Langfuse configured separately from prompts | No observability service is required to run an agent |

## What the application owns

- **Capabilities.** Register a factory per tool name. The factory receives the request context and returns a thin adapter that calls your API with the caller's token.
- **Output schemas.** Register a Pydantic model per name; the runtime passes it to the SDK as the output type.
- **Context.** Load what you already know the agent needs, and pass it in. The agent should not rediscover your data through tools.
- **Authorization.** Always, in the owning service. The runtime propagates identity; it does not decide.

## Deliberately not in this version

- Sub-agents, delegation and handoffs. One agent, one prompt, one model.
- Guardrails. Nothing had ever registered one; an application checks its own inputs and outputs.
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

## The supported starting format

A prompt snapshot stores the template as written, placeholders included, with the definition's variables beside it. That is the only format this package supports, and there is nothing to migrate from: no environment has ever run a build that published definitions, and the development databases that did are disposable.

An earlier development build stored *rendered* text instead and dropped the variables. Nothing in a row says which build wrote it - not even the date, since an old binary can publish at any time - so a local database that predates this format is recreated rather than inspected:

```bash
docker compose down -v && docker compose up -d postgres
asas-agent migrate
```

If that assumption is ever wrong for some registry, the symptom is loud rather than silent: a rendered snapshot read as a template demands a value for a placeholder that used to be literal text. The fix is to publish a new version from a current build, because a published version is immutable.

## Divergences from the engineering guideline

The guideline sketches a runtime API taking `input` plus a `business_context` object. This package takes `inputs`, which fill the prompt's placeholders, and an optional `message`. One convention rather than two means a developer never has to decide where a fact belongs, and adding a placeholder to a prompt cannot change what the other parameter means. The guideline's intent - the business service loads what it already knows and passes it in, rather than the agent rediscovering it - is unchanged.

## Runtime and publication safeguards

The runtime owns dependency merging, platform ceilings, and cancellation deadlines for every entry point. Langfuse retrieval and flush calls run outside the event loop. Cancelled runs leave buffered spans to the SDK exporter instead of extending their deadline to flush.

Publication resolves every name a definition uses without invoking a tool factory. A PostgreSQL transaction-scoped advisory lock serializes version allocation per agent, including the first draft. Promotion needs no lock of its own: a binding points at one published version and nothing else depends on it.
