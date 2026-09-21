# asas-agent Technical Design

**Artefact:** `docs/specs/asas-agent/design.md` | **Version:** 0.1 | **Date:** 21 September 2026 | **Reverse-engineered from `main` at `4e99b15`**

How the contract in `spec.md` is built. Every section cites the requirements it serves; a section citing none would be construction nobody asked for.

---

## 1. Components

| Component | Serves | Is |
|---|---|---|
| `registry/db.py` | R-REG-1, R-REG-6 | Two tables and an engine |
| `registry/repository.py` | R-REG-1…8, R-PUB-1…4 | Every read and write of a definition or a binding |
| `registry/validation.py` | R-PUB-1 | Resolves the names a definition uses, without invoking anything |
| `registry/lookups.py` | R-RUN-6…9 | Shares the one query every run starts with |
| `registry/capabilities.py` | R-CAP-1…5 | Names bound to factories, and the policy that refuses them |
| `registry/outputs.py` | R-RUN-10 | Names bound to Pydantic models |
| `integrations/prompts.py` | R-PR-1…9, R-PUB-2…4 | Fetching, rendering and freezing a prompt |
| `integrations/models.py` | R-PUB-5 | `provider:name` to something the SDK accepts |
| `runtime/factory.py` | R-RUN-1…3, R-TR-4 | A definition in, an SDK agent out |
| `runtime/runner.py` | R-RUN-2…5, R-TR-3…5 | One run, with its deadline and its trace |
| `api/app.py` | R-API-1…4 | The runtime over HTTP, behind an optional extra |
| `cli/main.py`, `migrate.py` | R-OPS-1, R-OPS-2 | Operator commands, and schema creation an application can call |

---

## 2. Data model

**D-1** (R-REG-1, R-REG-6) Two tables, and no more. `agent_definitions` holds every version of every agent, with the definition as a `JSONB` column. `agent_environment_bindings` holds one row per agent and environment, naming the version that environment runs.

**D-2** (R-REG-1) Nothing updates a definition's `config` after publication. Publication writes `status` and `published_at`, and the pinned prompt reference; there is no path that edits a published row's body.

**D-3** (R-REG-3) The binding table carries a foreign key to `(agent_key, version)`, so a binding cannot name a version that does not exist. Publication status is checked in the write path rather than by constraint, because the database has no view of it at that point.

**D-4** (R-REG-6) The binding's primary key is `(agent_key, environment)`, so one environment cannot run two versions of one agent.

**D-5** A `status` check constraint permits `draft`, `published` and `archived`. **`archived` is unreachable**: nothing in the package writes it. It is recorded here because the constraint implies a state the code does not have — see `verification.md`.

---

## 3. Version allocation and concurrency

**D-6** (R-REG-2) `create_draft` takes a transaction-scoped PostgreSQL advisory lock keyed on the agent, then reads `max(version) + 1`. The lock is released when the transaction ends, including on failure, because it is `pg_advisory_xact_lock` rather than a session lock.

**D-7** (R-REG-2) The lock is per agent, not per registry: two agents may allocate versions at the same time.

**D-8** (R-REG-4) Binding takes no lock of its own. A binding points at one published version and nothing depends on the shape of a graph, because there is no graph — see `spec.md` §3.

---

## 4. Publication

**D-9** (R-PUB-1) `DefinitionValidator` resolves the model provider, the model settings, every tool name and the output schema name. It calls `CapabilityRegistry.get`, never the capability's factory, so publishing cannot execute application code.

**D-10** (R-PUB-2) `pin_prompt` runs the shape check on the template it fetched, before any value is substituted, so a prompt that cannot instruct an agent is refused while it is still a draft.

**D-11** (R-PUB-3) A prompt with no versions is stored in the definition: `snapshot` for a text prompt, `snapshot_messages` for a chat prompt, with the definition's own variables beside it. The stored template keeps its placeholders, because the values that fill them belong to a request.

**D-12** (R-PUB-4) A prompt with versions is pinned by number, and the label is dropped, so the reference resolves to the same text forever.

---

## 5. Prompts

**D-13** (R-PR-8) Placeholders are found by the provider's own rule — find `{{`, take the next `}}`, strip — implemented here rather than delegated, so a file prompt and a service prompt with the same text render identically. Delegating to the provider's `compile()` was tried and dropped: it cannot take a variable named `self`, and it reports missing values only by leaving them in the text.

**D-14** (R-PR-3, R-PR-4) Both checks read the **unrendered** template. Scanning rendered output was tried and reverted: a value containing `{{...}}` was reported as a missing variable.

**D-15** (R-PR-9) A prompt path is resolved and compared against the resolved prompt directory, which refuses `..` and a symlink pointing outside it.

**D-16** A parsed prompt file is cached against `(path, mtime_ns, size)`, taken **before** the read, so a file written during the read is not cached under the stamp of the version that was not read. A replacement preserving timestamp and size (`cp -p`, a restored backup) looks unchanged; the docstring says so.

---

## 6. Shared lookups

**D-17** (R-RUN-7) `SharedAgentLookups` wraps the repository and keys in-flight queries on `(agent_key, environment)`. A run arriving while one is open awaits it.

**D-18** (R-RUN-8) The query is a task, awaited through `asyncio.shield`, and registration ends when the task finishes or a write invalidates it — never because the caller that started it was cancelled. Ending it on cancellation was tried and reverted: a run arriving between the cancellation and the answer opened a second query.

**D-19** (R-RUN-9) A write made through the wrapper detaches the in-flight query for that agent, so a run arriving after it starts a fresh one. Runs already waiting keep the answer they asked for.

**D-20** (R-RUN-6) Every answer is handed out as a copy with a deep-copied configuration.

**D-21** Nothing is stored after a query completes. A TTL cache was built and removed: measured against PostgreSQL, sharing the query took a 40-run fan-out from 128 ms to 5.4 ms, and keeping the answer bought a further 3.6 ms in exchange for invalidation, eviction, a generation fence and a standing question about staleness.

---

## 7. Running

**D-22** (R-RUN-2) The deadline is opened before the factory is called and rescheduled once the definition's own limit is known, so assembly is inside it.

**D-23** (R-RUN-4) The run's input is a list of message items: the prompt's own user message, then the caller's message if there is one.

**D-24** (R-TR-5) `RunConfig` carries only `tracing_disabled`. The run name and the caller's fields go to the observation this runtime opens, not to the SDK's trace, which may be exported elsewhere.

**D-25** (R-TR-2) After instrumenting, the runtime checks the SDK's processor list and removes every processor that exports to the SDK vendor's backend, whether the instrumentation attached, failed, or was already present. Trusting `instrument()` was tried and failed: it reports a version mismatch by logging and returning, so its silence meant nothing.

**D-26** (R-TR-4) The factory writes what it resolved into the context's trace metadata, and it writes **after** the caller's own fields are copied in, so a request cannot change what the record says about the run. It can still add a field of its own beside them: there is no list of names the runtime owns — see `verification.md` F-7.

---

## 8. External contracts

Each verified by execution against the pinned dependency, not from memory.

| Claim | How it was verified |
|---|---|
| The prompt service renders `str(value)`, and `None` as empty | Read `TemplateParser.compile_template` in the installed package; rendering compared against the equivalent LangChain path, identical |
| Strict structured output accepts `minItems`/`maxItems` | Converted a bounded model through the SDK's strict conversion: the keywords survive and are sent. An earlier claim that they are rejected was wrong and untested |
| A message item `{"role", "content"}` with `user` or `assistant` is valid SDK input | Built items through the SDK's converter and validated them |
| Instrumentation reports a version mismatch by logging, not raising | Ran it against a deliberately mismatched pair: `instrument()` returned `None` and left the vendor exporter installed |
| A transaction-scoped advisory lock is released when the transaction ends | 20 concurrent first drafts each received a distinct version, with no leaked locks |

---

## 9. Predicates

Every selection the code makes, and what else it matches.

| Predicate | Also matches | Consequence |
|---|---|---|
| `get_active`: join definitions to bindings on `(agent_key, version)` and filter by environment | **Any status**, including a row set to `archived` out of band | An archived version would still serve traffic while bound. Nothing in the package writes `archived`, so this is unreachable today — recorded in `verification.md` |
| `publish`: refuse `published` or `archived` | — | A draft is the only publishable state |
| `bind`: require `status == "published"` | — | A draft or an archived version cannot become live |
| Missing-value check: template placeholders minus supplied names | — | The empty template matches nothing, so a prompt with no placeholders needs no values |
| Unknown-input check: supplied names minus template placeholders | — | A definition's own variables are excluded first, so they are never reported as unknown |

---

## 10. Failure handling

**D-27** (R-API-3) The HTTP layer maps each failure to what the caller can do about it: unknown agent or binding → 404; refused tool or unknown output schema → 403; bad input → 400; a prompt that cannot instruct → 500, because the definition is broken rather than the service; an unreachable provider → 502; deadline → 504; turn limit → 422.

**D-28** (R-REG-8) Publication's three steps commit separately. A definition refused at publication leaves its draft; a failure at binding leaves a published version nothing runs. Neither is reachable by a request, and both are visible in `agent list`.

---

## 11. Security

**D-29** (R-CAP-2) A capability factory receives the request's context and returns a tool that closes over it, so a tool built for one caller cannot be reused for another.

**D-30** (R-API-2) The API key is compared with `secrets.compare_digest`. **An unset key disables authentication**, and `.env.example` ships it empty — recorded as a finding in `verification.md`, not a property to rely on.

**D-31** (R-TR-3) A run name is rejected if it contains a control or formatting character, exceeds 200 bytes, or begins with the prefix the runtime uses for an unnamed run.

---

## 12. Testing strategy

Unit tests for the registries, the schema and rendering; PostgreSQL tests against a disposable database for version allocation, publication and binding; SDK tests that run the real `Runner` against a transport that answers, so no request leaves. An external application exercises the whole path end to end and is cited in `verification.md`.

---

## 13. Open questions

1. **`archived` has no writer.** Either give it one (with a rule for what a bound archived version means) or remove it from the constraint and the literal.
2. **`get_active` does not filter on status.** Harmless while nothing archives; it would stop being harmless the moment something does.
3. **An unset API key disables authentication** rather than refusing to serve. Failing closed outside `dev` was raised in review and has not been done.
4. **No evaluation gate** between draft and published, which the guideline asks for.
5. **A request can add fields to the trace record**, which R-TR-4 forbids. Either narrow the requirement to overwriting, or name the fields the runtime owns and refuse a collision.
