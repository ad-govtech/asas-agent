# Repository Guidelines

## Project Structure & Module Organization

`src/asas_agent/` contains the Python package: `registry/` manages definitions and capabilities, `runtime/` assembles and executes agents, `integrations/` resolves prompts and models, and `api/` and `cli/` expose FastAPI and Typer interfaces. Alembic revisions live in `migrations/versions/`. Unit tests are in `tests/`. `examples/` contains customer-advisor and recruiting applications; recruiting prompts, JSON definitions, and browser assets live alongside its code. Read `docs/design.md` and `docs/agent-as-configuration-guideline.md` before architecture changes.

## Build, Test, and Development Commands

Run from the repository root:

- `uv venv && uv pip install -e ".[dev]"` — install an editable development environment; add `tracing` for Langfuse instrumentation.
- `uv run pytest` — run the test suite.
- `uv run ruff check .` — check style, imports, and common errors.
- `uv run ruff format .` — format Python with a 120-character line length.
- `uv build` — build source and wheel distributions using Hatchling.
- `docker compose up -d postgres` — start local PostgreSQL on port 5433.
- `uv run asas-agent migrate` — apply registry migrations using `DATABASE_URL`.
- `uv run asas-agent serve` — start the runtime API on port 8080 by default.

See `examples/recruiting/README.md` for console setup and seed commands.

## Coding Style & Naming Conventions

Target Python 3.11+, use four-space indentation, and follow existing type annotations and Pydantic models. Use `snake_case` for functions/modules, `PascalCase` for classes, and uppercase constants. Capability names use dotted identifiers such as `policy.search`; agent keys use hyphens such as `customer-advisor`. Ruff enables `E`, `F`, `I`, `UP`, and `B` rules.

## Testing Guidelines

Use pytest and pytest-asyncio, with automatic asyncio mode. Name files `test_*.py` and functions `test_<behavior>`. Add regression tests for schema validation, capability authorization, and changed runtime behavior. Keep unit tests independent of model credentials. PostgreSQL tests use `ASAS_TEST_DATABASE_URL` and isolated schemas; CI runs them. Run console security checks with `node --test tests/recruiting-security.test.mjs`. No coverage threshold is configured. Run a focused file with `uv run pytest tests/test_schema.py`.

## Commit & Pull Request Guidelines

The short Git history uses descriptive subjects without a strict prefix convention. Use concise, action-oriented subjects. PRs should explain behavior changes, list validation commands/results, link relevant issues, and describe configuration or migration impacts. Include screenshots for recruiting-console UI changes.

## Architecture & Configuration

Keep the runtime stateless and business authorization in the owning service. Agent definitions reference registered capabilities; never embed implementations or secrets. Preserve published-version immutability. Use `.env.example` for settings and keep credentials in ignored `.env`. File prompts are snapshotted on publish; Langfuse is optional and supports self-hosted deployments. Tracing is configured separately and defaults to off.
