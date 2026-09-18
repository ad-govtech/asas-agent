# Design notes

Built to the *Agent-as-Configuration Platform* engineering guideline (September 2026). This file records what the package implements, what it deliberately leaves to the calling application, and what is still open.

## The boundary

```
Business service            asas-agent                        Outside
─────────────────           ──────────────────────            ──────────────
authenticates the caller    resolves the definition           Postgres registry
authorizes the action       resolves the prompt               Langfuse prompts
loads mandatory data  ────► binds approved tools      ──────► OpenAI / gateway
applies business rules      builds the SDK agent              your business APIs
persists the outcome        runs it, records the trace        Langfuse traces
```

The runtime holds no business state and no session. Anything durable lives in Postgres or in the calling service.

## What the package decides

| Decision | Choice | Why |
|---|---|---|
| Definition store | Postgres, JSONB `config` column | Versioning, transactional publishing, auditability, no deploy to change an agent |
| Version model | Immutable rows, one binding per environment | Promotion and rollback move a pointer; a published version never changes |
| Validation | Pydantic, on save, on publish, and again on load | Bad configuration never reaches a model call |
| Prompts | Langfuse by label or version | Prompt text and its history stay out of the registry |
| Prompt pinning | Publishing resolves a label to the version live at that moment | `customer-advisor v2` always means the same thing |
| Tools | Named in configuration, implemented in the app | The database never carries code, URLs or credentials |
| Action tools | Refused unless the caller enables them | An LLM naming a tool is not authorization |
| Models | Registry of providers, with a capability table | A model that cannot call tools is refused at publish, not mid-run |
| Execution | OpenAI Agents SDK | The SDK is the engine; `AgentConfig` is our own format |
| Tracing | Langfuse, with agent, prompt, model and toolset metadata | Every production run can be reconstructed |

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
