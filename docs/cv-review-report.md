# CV Review CLI — what building it found

A real application built on `asas-agent`, to the plan in
[cv-review-cli-plan.md](cv-review-cli-plan.md), outside this repository at
`cv-review/`. Three agents, one tool, five CVs, eight scenarios.

**Result: 8/8 scenarios pass.** Two gaps in the package were found by building
it, both fixed and both in a pull request. Nothing in the application works
around the runtime any more.

## What was built

| Piece | What it is |
|---|---|
| `cv-extract` | CV text → `cv_profile_v1` (candidate, years, skills, roles, education) |
| `cv-assess` | profile + role → `cv_assessment_v1`, using the `role.requirements` tool |
| `cv-compare` | assessments → `cv_ranking_v1`, ordered with reasons |
| CLI | `setup`, `screen`, `batch`, `roles`, `versions`, `rollback` |
| Model | Any OpenAI-compatible endpoint, through the `gateway` provider |

File prompts, no Langfuse. Postgres for the registry. The application registers
one capability and three output schemas, and nothing else.

## Scenarios

```
PASS  1 extraction:   ada, 9 years, 5 skills
PASS  2 assessment:   ada=shortlist, brian=reject (gaps: missing python, missing pipelines)
PASS  3 tools:        erik=hold, gaps: missing sql, missing pipelines
PASS  4 batch:        5 reviewed, ranked ada > chen > erik > brian > dalia
PASS  5 concurrency:  40 runs, 40 distinct candidates, 0 leaks, 5 registry queries, 348 ms
PASS  6 versioning:   promoted to v2, rolled back to v1, no deployment
PASS  7 failures:     malformed answer -> ModelBehaviorError; unknown input -> PromptVariableError;
                      unknown agent -> RegistryError
PASS  8 cli:          profile: 8 years, python, sql, pipelines
```

Each asserts something checkable rather than judging prose. Three are worth
drawing out:

**Tools (3).** Erik has twelve years and is held rather than shortlisted,
because he is missing `sql` and `pipelines`. The minimum years and the
must-have list exist only behind `role.requirements`; an assessment that cited
them could only have got them by calling the tool. The runtime's tool loop, the
capability registry and the request-bound adapter are all exercised by that one
assertion.

**Concurrency (5).** Forty candidates at once, sharing one `RuntimeContext`:
every result matched its own candidate, and eighty agent runs resolved their
definitions with **5 registry queries** rather than 80. That is the shared
lookup doing what it was measured to do, in an application rather than a
benchmark.

**Failures (7).** Each failure arrives as something a caller can act on: a model
answering in the wrong shape is a `ModelBehaviorError` naming the missing field,
an input the prompt does not use is a `PromptVariableError` naming it, and an
unpublished agent is a `RegistryError` naming the environment.

## What the package made the application copy

Both are now fixed in [#10](https://github.com/ad-govtech/asas-agent/pull/10).

**Creating the registry tables.** `migrate` existed only as a CLI command, so
the application copied its Alembic wiring — and then found by traceback that the
migration starts an event loop of its own, so it cannot be called from inside
one. That is the first thing an async application tries. `migrate()` is now part
of the package and says what to do instead.

**Publishing an agent.** Create a draft, publish it, bind the environment: three
calls in a fixed order, written out by the package's own example, by the CLI,
and by this application. `repository.release(...)` is now those three steps.

## What was noticed but not changed

**The output schema name never reaches the model.** A product registers
`cv_profile_v1`, and every structured call goes out as `final_output`. A gateway
that routes or logs on schema name cannot tell two agents apart. This is the
Agents SDK's naming, not ours; worth knowing before someone relies on it.

**A dict crosses the wire as a Python repr.** Both a rendered input
(`{'candidate': 'ada', ...}`, which is deliberate and documented — it matches
Langfuse and LangChain) and a tool's dict result arrive at the model that way.
Real models read it; a product that cares should return a JSON string from its
tools.

**A prompt with no user message needs a caller message.** The contract is right
and the error says so, but it can only be discovered at run time: publication
knows the prompt's shape and cannot know whether the caller will send anything.

## What this did not test

**Model quality.** No credentials were available, so scenarios ran against a
deterministic stand-in that reads the same prompt a model would and answers in
the schema the runtime asked for, including failure modes. Every result above is
about the package — routing, tools, schemas, versions, concurrency, errors — and
none of it is about how well a model judges a CV.

To run the same application against a real model, change two variables and
nothing else:

```bash
MODEL_GATEWAY_URL=https://api.core42.ai/v1   MODEL_GATEWAY_KEY=...
```

What would change: the prose in `evidence` and `gaps`, and the ranking order
where candidates are close. What would not: the schemas, the tool call, the
version in force, or the error a bad answer produces — those are the package's,
and they are what these scenarios pin.

## Would a product build on this?

On this evidence, yes. The application is 500 lines and none of them work around
the runtime: it declares three agents as JSON, registers a tool and three
schemas, and calls `runtime.run`. Promotion and rollback are a row change.
Forty concurrent runs cost five registry queries and leak nothing between
requests. The two rough edges it found were both in the package's setup path,
both small, and both fixed by the application that found them.
