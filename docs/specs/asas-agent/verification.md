# asas-agent Verification

**Artefact:** `docs/specs/asas-agent/verification.md` | **Version:** 0.1 | **Date:** 21 September 2026 | **Against `main` at `4e99b15`**

Status lives only here. `spec.md` says what must be true; this says whether it is, and what proves it.

## Vocabulary

| Implementation | Meaning |
|---|---|
| **Met** | Implemented as specified |
| **Partial** | Implemented incompletely, or differently from the requirement |
| **Gap** | Not implemented |

| Verification | Meaning |
|---|---|
| **Enforced** | A test fails when the requirement is broken — **proven by mutation** |
| **Covered** | A test cites it; breaking the code has not been shown to fail it |
| **Observed** | Seen working by hand, or by an application outside this repository |
| **None** | Nothing checks it |

Evidence names a test file, a command, or a line of code. `pytest -q` on this revision: **175 passed**, of which 10 run against a disposable PostgreSQL database and are skipped unless `ASAS_TEST_DATABASE_URL` is set.

---

## Status

### The registry

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-REG-1 published definitions never change | Met | Covered | `test_repository_keeps_existing_versions_immutable`; no write path edits a published body |
| R-REG-2 version allocation, concurrent-safe | Met | Covered | `test_concurrent_first_drafts_receive_distinct_versions` — 20 concurrent drafts, distinct versions |
| R-REG-3 only a published version may be bound | Met | Covered | `test_invalid_publications_roll_back`; `bind` refuses any other status |
| R-REG-4 promotion and rollback are one operation | Met | **Observed** | Demonstrated against a live process: v8 → v9 → rolled back to v8, no restart |
| R-REG-5 no binding is an error naming both | Met | Covered | `RegistryError` in `get_active`; exercised by `test_shared_lookups.py` |
| R-REG-6 one version per environment | Met | Covered | Primary key on `(agent_key, environment)` |
| R-REG-7 `release` is ordered and not idempotent | Met | **Enforced** | `test_release_moves_the_environment_every_time_it_is_called` — release, release, roll back, release, and the rollback is gone |
| R-REG-8 a failed publication never becomes live | Met | Covered | `test_release_refuses_a_definition_that_could_not_run` asserts no binding and the draft remains |

### Publication

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-PUB-1 every name resolves, nothing is invoked | Met | **Enforced** | `test_publication_checks_tools_without_executing_them` — the factory raises if called |
| R-PUB-2 an unusable prompt is refused at publication | Met | Covered | `test_a_broken_prompt_is_refused_at_publication_not_on_every_run` |
| R-PUB-3 a file prompt is stored as written | Met | Covered | `test_publishing_a_chat_prompt_stores_its_messages_unrendered`; `test_published_file_prompt_survives_edits_and_removal` deletes the file and still resolves |
| R-PUB-4 a versioned prompt is pinned | Met | Covered | `test_langfuse_publication_pins_version_and_preserves_variables` |
| R-PUB-5 no model existence or capability check | Met | Covered | `test_configured_model.py`; the catalogue that did this was removed |

### Prompts

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-PR-1 one system, optionally one user | Met | Covered | `test_a_shape_other_than_one_system_and_one_user_is_refused` (3 shapes) |
| R-PR-2 instructions and opening message | Met | Covered | `test_a_chat_prompt_splits_into_instructions_and_opening_messages` |
| R-PR-3 a missing value is an error | Met | Covered | `test_a_variable_with_no_value_is_an_error_not_a_placeholder_sent_to_the_model`, both providers |
| R-PR-4 an unused value is refused | Met | Covered | `test_what_a_request_may_fill_is_what_is_left_unanswered` — `{"unknown": "x"}` raises `PromptVariableError` naming what the prompt does ask for |
| R-PR-5 a request cannot replace a definition's value | Met | Covered | `test_a_request_cannot_replace_a_value_the_published_definition_sets` |
| R-PR-6 what a request may fill is derived | Met | Covered | `test_what_a_request_may_fill_is_what_is_left_unanswered` |
| R-PR-7 substitution is single-pass | Met | Covered | `test_render_is_a_single_pass_over_each_message`; `test_a_filled_value_that_looks_like_a_placeholder_is_left_alone` |
| R-PR-8 rendering matches the provider | Met | Covered | `test_both_providers_render_a_value_the_same_way`; the provider's parser was read to match it |
| R-PR-9 no reading outside the prompt directory | Met | Covered | `test_file_prompt_cannot_escape_directory`, `…cannot_follow_symlink_outside_directory` |

### Running

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-RUN-1 the bound version, at start | Met | **Observed** | A running process moved from v8 to v9 on the next run after promotion |
| R-RUN-2 the smallest deadline, covering assembly | Met | **Enforced** | `test_runtime_deadline_cancels_pending_work[assembly|execution]`; the execution case was rewritten after it was shown to pass on an unrelated error |
| R-RUN-3 the smallest turn limit | Met | Covered | `test_direct_runtime_merges_dependencies_and_clamps_limits`; `test_real_runtime_stops_tool_loop_at_platform_ceiling` |
| R-RUN-4 something to answer, or refuse | Met | Covered | `test_a_run_with_nothing_to_say_is_refused` |
| R-RUN-5 inputs are bounded | Met | Covered | `test_inputs_larger_than_the_ceiling_are_refused` |
| R-RUN-6 each run gets its own configuration | Met | Covered | `test_runs_sharing_a_query_do_not_share_its_configuration` |
| R-RUN-7 one query per moment | Met | **Enforced** | `test_a_fan_out_asks_once_not_once_per_branch`; mutation removing single-flight is caught |
| R-RUN-8 a cancelled run leaves the query alone | Met | **Enforced** | `test_a_run_arriving_after_the_first_gave_up_joins_the_query_it_left_running`; `test_repeated_deadlines_do_not_pile_up_queries` |
| R-RUN-9 a local write detaches the in-flight query | Met | **Enforced** | `test_a_promotion_here_detaches_the_query_it_affects`; verified against PostgreSQL with a real rollback mid-query |
| R-RUN-10 a validated result, or failure | Met | Covered | `test_a_definition_naming_a_model_and_a_schema_returns_a_validated_object`; `test_an_answer_that_does_not_fit_the_schema_is_an_error_not_a_result` |

### Tools

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-CAP-1 configuration names, never implements | Met | Covered | `AgentConfig` has no field that could hold code; `test_unknown_fields_are_refused` |
| R-CAP-2 a tool is built per request | Met | Covered | `test_a_registered_capability_is_built_with_the_request_context` |
| R-CAP-3 an action tool needs enabling | Met | Covered | `test_action_tools_need_to_be_enabled_for_the_caller` |
| R-CAP-4 a caller may narrow, never widen | Met | Covered | `test_a_caller_can_be_limited_to_fewer_tools_than_the_definition_lists` |
| R-CAP-5 an unknown capability names what is registered | Met | Covered | `test_an_unknown_capability_is_refused` |

### Tracing

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-TR-1 off by default | Met | Covered | `test_default_platform_runs_without_langfuse`; `tracing_provider` defaults to `none` |
| R-TR-2 no foreign exporter left installed | Met | **Enforced** | `test_tracing_safety.py` — instrumentation that returns, raises, is missing, is already present, and one that does attach |
| R-TR-3 a run is named, and the name is plain | Met | Covered | `test_a_run_name_that_cannot_be_trusted_is_refused` (blank, `agent:` prefix, over-long, control and zero-width characters) |
| R-TR-4 the runtime's record is its own | **Partial** | **Observed** | The factory's `update()` lands **after** the caller's dict, so a request cannot overwrite a field — but nothing stops it **adding** one. Measured: `agent_key="LIE"` became `scorer`; `smuggled="yes"` survived into the metadata. See finding F-7 |
| R-TR-5 nothing of the caller's in the SDK trace | Met | None | `runner.py` builds `RunConfig(tracing_disabled=…)` and passes no `workflow_name`, `group_id` or `trace_metadata`. True by construction; `test_prompts.py` asserts only the `tracing_disabled` flag |

### Interfaces

| Requirement | Implementation | Verification | Evidence |
|---|---|---|---|
| R-API-1 HTTP is optional | Met | **Enforced** | `test_serve_says_what_to_install_when_the_extra_is_missing`; verified in a real default install — uvicorn present, FastAPI absent |
| R-API-2 constant-time key check when set | **Partial** | Covered | `test_api_auth_uses_injected_platform_settings`. **An unset key disables authentication**, and `.env.example` ships it empty — see finding F-3 |
| R-API-3 a status per kind of failure | Met | Covered | `api/app.py` mapping; `test_the_api_answers_a_caller_error_and_a_broken_definition_differently` |
| R-API-4 stateless | Met | None | True by construction — nothing durable is held — but nothing asserts it |
| R-OPS-1 an application can create the schema | Met | Covered | `test_migrate.py`, including the refusal inside an event loop |
| R-OPS-2 settings from the environment | Met | Covered | `test_dotenv_key_reaches_real_openai_client` |

### Invariants

| Invariant | Status | Evidence |
|---|---|---|
| INV-1 the database never carries an implementation | Holds | No field in `AgentConfig` accepts code; `extra="forbid"` |
| INV-2 a published version never changes | Holds | R-REG-1, R-PUB-3, R-PUB-4 all Met |
| INV-3 identity travels in the context | Holds | R-CAP-2 Met; the prompt receives no identity field |
| INV-4 a request cannot misreport what ran | **Partial** | R-PR-5 Met; R-TR-4 Partial — a request cannot *change* a recorded field but can *add* one beside it (F-7) |
| INV-5 nothing durable in the runtime | Holds | R-RUN-7 stores nothing after a query; no session state |
| INV-6 model capability is the provider's answer | Holds | R-PUB-5 Met since the catalogue was removed |

---

## Findings from the audit

Written down rather than fixed silently, because each is a decision someone should make.

**F-1 — `archived` is a state with no writer.** The check constraint and the `Status` literal permit it; nothing in the package writes it, and no requirement mentions it. *Either give it a writer and a rule for what a bound archived version means, or remove it.* (`design.md` D-5, open question 1.)

**F-2 — `get_active` does not filter on status.** It joins definitions to bindings and filters by environment only, so a row archived out of band would still serve traffic while bound. Unreachable today because of F-1; the two findings protect each other, which is not the same as being safe. (`design.md` §9, open question 2.)

**F-3 — an unset API key disables authentication.** `if expected and not compare_digest(...)` means a blank key authenticates everyone, and `.env.example` ships it blank. R-API-2 is recorded **Partial** for this reason. Failing closed outside `dev` was raised in review and not done. (Open question 3.)

**F-4 — one test passed vacuously and has been removed.** `test_two_sub_agents_cannot_share_a_tool_name` asserted a rule that no longer exists: with sub-agents removed, it raised because the field is unknown, not because two tools shared a name. It is deleted in this revision; `test_unknown_fields_are_refused` already covers what it was really testing.

**F-5 — R-API-4 has no test.** Statelessness is true by construction and asserted nowhere. A test that runs the same agent through two independently built platforms would cost little.

**F-7 — a request can add fields to the runtime's trace record.** R-TR-4 asks that a request be able to neither overwrite nor add; only overwriting is prevented, and only by the order of two `dict.update` calls rather than by a rule. Anything a caller puts in `RuntimeContext.trace_metadata` under a name the factory does not use is copied verbatim into the Langfuse metadata. *Either narrow the requirement to overwriting, or give the runner a list of names it owns and refuse a request that collides with it.* (`design.md` D-26, open question 5.)

**F-6 — the guideline's evaluation gate is not built.** Publication is the hook it asks for, and there is no runner. Recorded in `spec.md` §3 as out of scope rather than left as an implied promise.

---

## Mechanical checks

Run on every revision of this spec, because reading has missed each of these at least once elsewhere:

```bash
# every id declared in spec.md appears in verification.md exactly once, and vice versa
grep -o '\bR-[A-Z]\+-[0-9]\+' docs/specs/asas-agent/spec.md | sort -u > /tmp/declared
grep -o '\bR-[A-Z]\+-[0-9]\+' docs/specs/asas-agent/verification.md | sort -u > /tmp/status
diff /tmp/declared /tmp/status

# every id is contiguous within its prefix
grep -o '\bR-[A-Z]\+-[0-9]\+' docs/specs/asas-agent/spec.md | sort -u -t- -k2,2 -k3,3n

# every test named as evidence exists, as a function or as a file
grep -o 'test_[a-z_]*' docs/specs/asas-agent/verification.md | sort -u |
  while read -r t; do
    grep -rq "def $t" tests/ || [ -f "tests/$t.py" ] || echo "missing: $t"
  done
```

Result on this revision: ids contiguous, declared and status sets identical (48), every
named test present apart from the one F-4 records as deliberately deleted. The last check earlier reported three tests that do not exist —
cited from an earlier shape of the package rather than from this one. The rows that
named them are the three corrected above; F-7 came out of checking what the code does
instead.
