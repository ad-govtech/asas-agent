# CV Review CLI — plan

A real application built on `asas-agent`, to find out what the package is like
to use before a product commits to it. It is deliberately the same shape as the
work the AI engine does today: read a CV, judge it against a role, compare
candidates.

The application lives outside this repository. The package should be usable by
something that is not an example inside it.

## What it does

```
cv-review setup                       migrate the registry, publish the agents
cv-review screen CV --role ROLE       one candidate, one role
cv-review batch DIR --role ROLE       every CV in a folder, ranked
cv-review versions                    what each environment runs
cv-review rollback AGENT VERSION      put a previous version back
```

## Agents

Three, mirroring the engine's parse → score → compare shape.

| Agent | Input | Output schema | Tools |
|---|---|---|---|
| `cv-extract` | CV text | `cv_profile_v1`: years, skills, roles, education | none |
| `cv-assess` | profile + role | `cv_assessment_v1`: recommendation, evidence, gaps | `role.requirements` |
| `cv-compare` | several assessments | `cv_ranking_v1`: ordered candidates with reasons | none |

`cv-assess` takes a tool on purpose: the capability registry is the part of the
package a product cannot avoid, so it should be exercised rather than described.

## What this is testing

The package, not the model. Each scenario has an answer that can be checked
without judging prose:

1. **Extraction** — a CV with known contents produces a profile with those contents.
2. **Assessment** — a strong CV is shortlisted, a weak one is not, and the evidence cites the CV.
3. **Tools** — the assessment reflects requirements only the tool could supply.
4. **Batch** — ten CVs, ranked, each result matching its own candidate.
5. **Concurrency** — forty at once, sharing one context: no cross-request leakage, one registry query.
6. **Versioning** — change a prompt, publish, promote, see the change; roll back, see it revert.
7. **Failure** — a malformed answer, a missing input, an unknown agent, a model that refuses: each fails in a way the caller can act on.

## Models

Any OpenAI-compatible endpoint, through the `gateway` provider. The scenarios
run against a deterministic local model so they can be repeated and asserted;
the same application runs against Core42 or OpenAI by changing two environment
variables, and the report says which results would change.

## Prompts and registry

File prompts, no Langfuse: that path should carry a real application on its own.
Postgres for the registry.

## What a good result looks like

Every scenario passes, and the report names what the package made awkward - the
parts where an application had to work around the runtime rather than use it.
Those become issues or a pull request against the package.
