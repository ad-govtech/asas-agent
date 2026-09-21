# asas-agent Module Specification

**Artefact:** `docs/specs/asas-agent/spec.md` | **Version:** 0.1 | **Date:** 21 September 2026 | **Status:** Reverse-engineered from `main` at `4e99b15`

**Reverse-engineered, not drafted ahead of the code.** Every requirement below was read out of the implementation and its tests, then checked against them. Where the code does something the requirement would not permit, the requirement records what the code does and `verification.md` says so.

**Construction lives in `design.md`; status lives only in `verification.md`.** A sentence here should stay true if this were rewritten in another language, on another database, against another provider.

**Ids are append-only.** `R-XXX-n` keeps the meaning it was allocated. New requirements take the next free number in their prefix, even when that puts them out of reading order.

**Cited:** `docs/agent-as-configuration-guideline.md` (the blueprint this was built to), `docs/design.md` (decisions and divergences).

---

## 1. Why

### 1.1 The problem

An agent is a prompt, a model, a set of tools and some limits. In a normal service all four are code, so changing any of them means a pull request, a review, a build and a deployment — and rolling one back means the same again, under time pressure, at the moment it is least welcome.

Three things follow, and each is a different kind of wrong.

**Changing behaviour requires shipping software.** Rewording an instruction and deploying a container are the same act, so the people who understand the wording cannot make the change, and the people who can are not the ones who should judge it.

**Nothing says what actually ran.** When an answer is wrong, the question "which prompt, which model, which version" has no answer a week later, because the artefacts that produced it were a branch, a build and a running process.

**Rollback is a deployment.** The fastest way to undo a bad agent is to redeploy the previous one — minutes at best, and only if that build still exists.

### 1.2 What this module owns

It owns **what an agent is, which version of it is live, and what happens when one runs**: the definitions, their versions, the environment bindings, prompt resolution, and one execution on the OpenAI Agents SDK.

| Not ours | Owner | Why the seam is here |
|---|---|---|
| Business state and business rules | The calling service | The runtime holds nothing durable of the business's; it assembles an agent and runs it |
| Authorization | The calling service | A model naming a tool is not authorization. The runtime carries identity and refuses tools by policy; it never decides whether an action is permitted |
| What a tool does | The calling service | Configuration names a capability; the code behind it is the application's, and never the database's |
| Whether a model can do what is asked | The provider | Asking a model for something it cannot do is answered by the provider, in its own words, at the first call |
| Conversation history | The caller | Every run is one shot; nothing is stored between them |

### 1.3 Done looks like

1. **A new agent is added by writing a row.** No deployment, provided its output schema and tools already exist in the application.
2. **A prompt is changed by publishing a version and promoting it.** A process already running picks it up on its next run.
3. **Rolling back moves a pointer.** The previous version is still there, unchanged, because a published version never changes.
4. **A trace says what ran** — agent key and version, prompt name and version, model, toolset — and a request cannot make it say anything else.
5. **A fan-out costs one registry query**, and no run can see another's data.

### 1.4 Vocabulary

| Term | Means | Not to be confused with |
|---|---|---|
| **Definition** | One version of one agent, as data | The agent, which is the key: a definition is a version of it |
| **Draft** | A definition that has been written but not locked | Published — a draft runs nowhere |
| **Published** | Locked, validated, never changed again | Live. Publishing does not make anything run |
| **Binding** | The row saying which published version an environment runs | The definition. Promotion and rollback move this, and nothing else |
| **Promotion** | Pointing an environment at a published version | Publication. They are separate acts, deliberately |
| **Capability** | A name in configuration bound to code in the application | A tool. The tool is built per request from the capability |
| **Input** | A value that fills a `{{placeholder}}` in the prompt | A message — what the caller says to the agent |
| **Run** | One execution of one agent | A conversation. There is no second turn from the caller |

---

## 2. The model

### 2.1 Entities

| Entity | Is | Identified by | Lifetime |
|---|---|---|---|
| **Agent** | A name a caller addresses | Its key (`cv-review`) | Forever; it is a name, not a row |
| **Definition** | One version of an agent, as data | Agent key + version | Immutable once published |
| **Binding** | Which version an environment runs | Agent key + environment | Moves; one per pair |
| **Capability** | A tool name bound to a factory in the application | Its key (`policy.search`) | The process's lifetime |
| **Output schema** | A typed result an agent may return | Its name (`cv_assessment_v1`) | The process's lifetime |
| **Prompt** | Instructions and an opening message | Name, plus a version or a stored snapshot | Frozen into the definition when published |

### 2.2 The lifecycle of a definition

```
            create_draft              publish                 bind
   (nothing) ──────────► draft ──────────────► published ───────────► live in an environment
                           │                       │                        │
                           │ refused at publish    │ never changes          │ bind elsewhere
                           ▼                       ▼                        ▼
                      stays a draft          a new version            moved, not edited
```

**R-REG-1** A published definition MUST NOT change.
**Why:** it is the only thing a trace can name a week later.

**R-REG-2** When a draft is created, the system MUST allocate the next version for that agent, and concurrent creations for one agent MUST receive distinct versions.

**R-REG-3** When an environment is bound, the system MUST refuse a version that is not published.
**Why:** a draft has not been validated; nothing unvalidated may become live.

**R-REG-4** Promotion and rollback MUST be the same operation: moving a binding to an already-published version.

**R-REG-5** When a run asks for an agent in an environment with no binding, the system MUST fail naming the agent and the environment.

**R-REG-6** An environment MUST run exactly one version of an agent at a time.

**R-REG-7** `release` MUST perform draft, publication and binding in that order, and MUST NOT be idempotent: every call adds a version and moves the binding.
**Why:** it is a deployment step. Called on start-up it would undo a deliberate rollback at the next restart.

**R-REG-8** A definition that fails validation at publication MUST NOT become live.

### 2.3 Publication is where configuration is checked

**R-PUB-1** When a definition is published, the system MUST resolve every name it uses — its tools, its output schema, its model provider, its model settings — without invoking a tool factory or calling a model.

**R-PUB-2** When a definition is published, the system MUST refuse a prompt that cannot instruct an agent.
**Why:** otherwise a broken definition is discovered by every request that uses it, as a failure that looks like an outage.

**R-PUB-3** When a prompt has no versions of its own, publication MUST store the prompt as written — placeholders included — in the definition.
**Why:** a runtime server then needs no access to prompt files, and editing a file cannot change a published agent.

**R-PUB-4** When a prompt provider has versions, publication MUST record the exact version resolved at that moment.

**R-PUB-5** The system MUST NOT verify at publication that a named model exists or supports what is asked.
**Why:** that is the provider's answer, and it gives it at the first call. Recorded because the opposite was once claimed.

### 2.4 A prompt, and what fills it

**R-PR-1** A prompt MUST be one system message, optionally followed by one user message; any other shape MUST be refused.
**Why:** the shape a reader can hold in their head, and the only one any product using this has needed.

**R-PR-2** The system message MUST become the agent's instructions, and the user message MUST open the run.

**R-PR-3** When a placeholder has no value, the system MUST fail naming it, and MUST NOT send the placeholder to the model.

**R-PR-4** When a request supplies a value the prompt does not use, the system MUST refuse it, naming what the prompt does ask for.
**Why:** it would not reach the model, so sending it is a mistake, not a no-op.

**R-PR-5** A request MUST NOT replace a value the published definition sets.
**Why:** definition values reach the system instructions; a caller able to rewrite one could rewrite a published agent's instructions.

**R-PR-6** What a request may fill MUST be derived — the prompt's placeholders, less what the definition answers — and MUST NOT require separate configuration.

**R-PR-7** A value MUST be substituted once, and its content MUST NOT be scanned for placeholders.
**Why:** a CV or a job description may contain `{{...}}`; that is data.

**R-PR-8** A value MUST render as its provider renders it: `str(value)`, and nothing at all for `None`.
**Why:** the same prompt text must produce the same call whether it came from a file or from the prompt service.

**R-PR-9** A prompt file MUST NOT be read from outside the configured prompt directory.

### 2.5 A run

**R-RUN-1** A run MUST use the version its registry lookup read, and a lookup MUST NOT reuse an answer once it has returned.
**Why:** the alternative is a cache, and a cache is a standing question about how stale a binding may be.

**R-RUN-11** A run that joins a lookup already in flight MAY be given the version that was live when that lookup started, so a promotion made in another process MAY be missed by at most the runs that arrive during one query; the run after it MUST be current.
**Why:** closing this window means distributed invalidation, which costs more than the window it protects. It is bounded by one query and stated so that nobody plans around a guarantee that is not there.

**R-RUN-2** A run MUST be bounded by the smallest of the caller's deadline, the definition's, and the platform ceiling, and that deadline MUST cover assembling the agent as well as executing it.

**R-RUN-3** A run's turn limit MUST be the smallest of the caller's, the definition's, and the platform ceiling.

**R-RUN-4** A run MUST start from the prompt's own user message, the caller's message, or both; when there is neither, the system MUST refuse it.

**R-RUN-5** The system MUST refuse inputs larger than the configured ceiling.
**Why:** they reach the instructions, which are re-sent on every turn.

**R-RUN-6** Each run MUST receive its own copy of the definition.
**Why:** runs share a lookup, and a configuration object is mutable.

**R-RUN-7** Runs in one process whose lookups for the same agent and environment overlap in time MUST share one registry query.
**Why:** a fan-out asks the same question dozens of times in a second. Runs in other processes, and runs whose lookups do not overlap, each ask their own.

**R-RUN-8** A run that is cancelled MUST NOT cancel a query other runs are waiting on, and MUST NOT prevent the next run from using its answer.

**R-RUN-9** When a promotion is made in this process, a run arriving afterwards MUST NOT be served by a query that started before it.

**R-RUN-10** The system MUST return a result validated against the definition's output schema, or fail.

### 2.6 Tools

**R-CAP-1** Configuration MUST name capabilities and MUST NOT carry code, SQL, URLs or credentials.

**R-CAP-2** A tool MUST be built for one request, closing over that caller's context.
**Why:** one caller's token must never be reused for another.

**R-CAP-3** A tool that changes data MUST be refused unless the caller enables action tools for that request.
**Why:** a model naming a tool is not authorization.

**R-CAP-4** A caller MAY narrow the definition's toolset for a request, and MUST NOT widen it.

**R-CAP-5** When configuration names a capability the application has not registered, the system MUST fail naming what is registered.

### 2.7 What a trace says

**R-TR-1** Tracing MUST be off by default, and no path in the default configuration may require credentials for an observability service.

**R-TR-2** When an internal trace backend is selected, the system MUST leave no processor exporting to the agent SDK vendor's backend, whether the instrumentation attached, failed, was already present, or is not installed.
**Why:** the SDK installs that exporter by default, so selecting a backend is otherwise an addition rather than a choice.

**R-TR-7** Selecting an internal trace backend MAY replace trace processors the embedding application installed before the runtime was built, and an application that installs its own MUST do so afterwards.
**Why:** the instrumentation library attaches exclusively — it replaces the SDK's processor list rather than adding to it — so this is not the runtime's choice to make on its host's behalf. Stated as an ordering constraint an application can act on, rather than left to be discovered. Changing it is `spec.md` P-4.

**R-TR-3** A run MUST be named — by the caller, or after its agent — and the name MUST be plain text, bounded in length, and MUST NOT claim the form the runtime uses for an unnamed run.
**Why:** an evaluation harness reads the name to decide which agent a generation belongs to.

**R-TR-4** The system MUST record what ran — agent key and version, prompt name and version, which inputs were filled, model, toolset, tenant — and MUST record what it resolved, whatever the caller supplied under those names.
**Why:** a trace is evidence of what ran, so the runtime's own fields are the runtime's answer and not a caller's suggestion.

**R-TR-6** The system MUST carry an embedding application's own trace fields through beside its record.
**Why:** the application knows things the runtime does not — a job id, a batch, an experiment arm — and this is the place to put them. The embedding application is trusted code; the HTTP interface does not expose this field, so it is not a path from a request.

**R-TR-5** The system MUST NOT put a caller's name or fields into the SDK's own trace.
**Why:** that trace may be exported somewhere the deployment did not choose.

### 2.8 Interfaces

**R-API-1** The HTTP interface MUST be optional: an application that embeds the runtime MUST NOT need a web framework.

**R-API-2** When an API key is configured, the HTTP interface MUST require it and MUST compare it in constant time.

**R-API-3** The HTTP interface MUST distinguish what the caller can fix from what it cannot: an unknown agent, a refused tool, a bad input, a broken definition, an unreachable provider, a deadline and a turn limit MUST each be a different status.

**R-API-4** The runtime MUST be stateless, so instances may be added without coordination.

**R-OPS-1** An application MUST be able to create the registry schema itself, and MUST be told what to do instead when it calls that from inside an event loop.

**R-OPS-2** Settings MUST come from the environment, and a `.env` file MUST be read when present.

### 2.9 Invariants

| # | Invariant | Requirements that carry it |
|---|---|---|
| INV-1 | The database never carries an implementation | R-CAP-1, R-CAP-5 |
| INV-2 | A published version never changes | R-REG-1, R-REG-3, R-PUB-3, R-PUB-4 |
| INV-3 | Identity travels in the context, never in prompt text | R-CAP-2, R-PR-5 |
| INV-4 | A request cannot misreport what ran | R-TR-4, R-PR-5 |
| INV-5 | Nothing durable lives in the runtime | R-API-4, R-RUN-7 |
| INV-6 | What a model can do is the provider's answer | R-PUB-5 |

---

## 3. Out of scope

Recorded because each was considered and removed or never built, and because an absence with no reason attracts a reimplementation.

| Not built | Why |
|---|---|
| **Sub-agents, delegation, handoffs** | Removed. Nothing published anywhere used them, and they brought a graph, cycle detection, per-child budgets and a lock on every promotion |
| **Guardrails** | Removed. The registry had no members: nothing in the package, the examples or the tests ever registered one |
| **A model capability catalogue** | Removed. It required every deployment to track a provider's releases, and refused unlisted models before anything was tried (R-PUB-5) |
| **Conversation storage** | A run is one shot. A conversation store is the caller's |
| **Per-tenant agent variants** | One definition per environment |
| **An evaluation gate between draft and published** | The guideline asks for it; publication has the hook and no runner |
| **Dynamic MCP discovery** | An MCP server can be a capability; discovering one from configuration is not supported |
| **A visual agent builder** | The CLI and the definition JSON are the interface |

---

## 4. Proposals

Not requirements. Each is a change someone might want, written here so that reading
§2 tells you what the module does and not what an author thought it should do. Nothing
below is a gap; promoting one means giving it the next free id in its prefix.

| Proposal | What it would change | Argument against |
|---|---|---|
| **P-1 Fail closed when no API key is set** | R-API-2 is conditional: with no key configured, the HTTP interface serves everyone. This would refuse to start outside `dev` unless a key is set | It is one line in a deployment's own configuration, and a runtime that refuses to start is a new way to have an outage. Raised in review and not done |
| **P-2 Name the fields the runtime owns and refuse a collision** | R-TR-4 keeps the runtime's answer by writing last. This would refuse a request that supplies one of those names, rather than quietly replacing it | The caller here is the embedding application, which is trusted code, and the HTTP interface does not expose the field at all. It would turn an extension point into an allowlist to maintain |
| **P-3 Map the provider client's own exceptions** | R-API-3 asks for an unreachable provider to have its own status; only the package's `ModelError` is mapped, so the SDK client's `APIConnectionError` becomes 500 | Real, and the smallest of these to fix — see `verification.md` F-8 |
| **P-4 Instrument non-exclusively** | `instrument(exclusive_processor=False)` adds a processor instead of replacing the list; the sweep that follows would then remove the SDK's exporter and leave an application's processors standing, satisfying R-TR-2 without R-TR-7's ordering constraint | Untested here, and it makes the runtime's tracing depend on what else is installed. It is the obvious answer to R-TR-7 and should be measured before it is believed |
