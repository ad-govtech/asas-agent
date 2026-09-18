"""`asas-agent`: set up the registry, manage versions, run the runtime.

asas-agent init        write .env, an example agent and a prompt
asas-agent migrate     create the registry tables
asas-agent doctor      check database, prompts, models and capabilities
asas-agent agent …     add, publish, promote and roll back versions
asas-agent serve       start the runtime API
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

import typer

from asas_agent.config import get_settings
from asas_agent.registry.schema import AgentConfig

app = typer.Typer(help="Agents as configuration: a Postgres registry, Langfuse prompts, a stateless runtime.")
agent_app = typer.Typer(help="Manage agent versions and environment bindings.")
app.add_typer(agent_app, name="agent")

EXAMPLE_AGENT = {
    "schema_version": 1,
    "name": "Customer Advisor",
    "description": "Explains a decision to a customer and proposes what they can do next.",
    "prompt": {"name": "agents/customer-advisor", "label": "production"},
    "model": {"provider": "openai", "name": "gpt-5.6-sol", "settings": {}},
    "tools": [],
    "sub_agents": [],
    "runtime": {"max_turns": 6, "timeout_seconds": 30},
    "output": {"schema": None},
    "guardrails": [],
}

EXAMPLE_PROMPT = """You explain government decisions to the people they affect.

Use only the facts in the context you are given. If a fact is missing, say so
rather than guessing. Write in plain language, in the language of the request.

Answer with: what was decided, why, and what the person can do next.
"""


def _run(coro):
    return asyncio.run(coro)


def _platform(require_prompts: bool = True):
    from asas_agent.bootstrap import build_platform

    return build_platform(require_prompts=require_prompts)


def _load_config(path: Path) -> AgentConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    return AgentConfig.model_validate(data)


@app.command()
def init(
    directory: Path = typer.Option(Path("."), "--dir", help="Where to write the files."),
    database_url: str = typer.Option("", "--database-url", help="Postgres URL to write into .env."),
) -> None:
    """Write .env, an example agent definition and its prompt."""
    directory.mkdir(parents=True, exist_ok=True)
    env_path = directory / ".env"

    if env_path.exists():
        typer.echo(f"{env_path} exists, leaving it alone")
    else:
        dsn = database_url or "postgresql://postgres@localhost:5432/asas_agent"
        env_path.write_text(
            "\n".join(
                [
                    f"DATABASE_URL={dsn}",
                    "ASAS_ENVIRONMENT=dev",
                    f"ASAS_API_KEY={secrets.token_urlsafe(24)}",
                    "",
                    "# Prompts: langfuse everywhere shared, file for local work",
                    "ASAS_PROMPT_PROVIDER=file",
                    "ASAS_PROMPT_DIR=prompts",
                    "LANGFUSE_PUBLIC_KEY=",
                    "LANGFUSE_SECRET_KEY=",
                    "LANGFUSE_HOST=https://cloud.langfuse.com",
                    "",
                    "OPENAI_API_KEY=",
                    "MODEL_GATEWAY_URL=",
                    "MODEL_GATEWAY_KEY=",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        typer.echo(f"Wrote {env_path}")

    prompt_path = directory / "prompts" / "agents" / "customer-advisor.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    if not prompt_path.exists():
        prompt_path.write_text(EXAMPLE_PROMPT, encoding="utf-8")
        typer.echo(f"Wrote {prompt_path}")

    agent_path = directory / "agents" / "customer-advisor.json"
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    if not agent_path.exists():
        agent_path.write_text(json.dumps(EXAMPLE_AGENT, indent=2) + "\n", encoding="utf-8")
        typer.echo(f"Wrote {agent_path}")

    typer.echo("\nNext: asas-agent migrate  →  asas-agent agent add customer-advisor  →  asas-agent serve")


@app.command()
def migrate(
    revision: str = typer.Option("head", help="Target revision."),
) -> None:
    """Create or update the registry tables."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    if not settings.database_url:
        raise typer.BadParameter("DATABASE_URL is not set. Run `asas-agent init` first.")

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, revision)
    typer.echo(f"Registry is at {revision}")


@app.command()
def doctor() -> None:
    """Check everything the runtime needs before it serves traffic."""
    from sqlalchemy import text

    settings = get_settings()
    problems: list[str] = []

    async def check() -> None:
        platform = _platform(require_prompts=False)
        try:
            async with platform.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                tables = await connection.execute(
                    text("SELECT to_regclass('public.agent_definitions') IS NOT NULL AS ready")
                )
                ready = tables.scalar()
            typer.echo(f"database          ok ({'tables present' if ready else 'run asas-agent migrate'})")
            if not ready:
                problems.append("The registry tables do not exist yet")

            bindings = await platform.repository.bindings() if ready else []
            typer.echo(f"agents bound      {len(bindings)}")
        finally:
            await platform.close()

    try:
        _run(check())
    except Exception as exc:  # noqa: BLE001 - the point of doctor is to report
        typer.echo(f"database          failed: {exc}")
        problems.append(str(exc))

    if settings.prompt_provider == "langfuse":
        if settings.langfuse_configured:
            typer.echo(f"prompts           langfuse at {settings.langfuse_host}")
        else:
            typer.echo("prompts           langfuse keys are missing")
            problems.append("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set")
    else:
        typer.echo(f"prompts           files in {settings.prompt_dir} (local use only)")

    typer.echo(f"openai key        {'set' if settings.openai_api_key else 'missing'}")
    typer.echo(f"model gateway     {settings.gateway_base_url or 'not configured'}")

    from asas_agent.registry.capabilities import registry as capabilities
    from asas_agent.registry.outputs import registry as outputs

    typer.echo(f"capabilities      {', '.join(capabilities.keys()) or 'none registered'}")
    typer.echo(f"output schemas    {', '.join(outputs.keys()) or 'none registered'}")

    if problems:
        typer.echo("\nFix these before serving traffic:")
        for problem in problems:
            typer.echo(f"  - {problem}")
        raise typer.Exit(code=1)


@agent_app.command("add")
def agent_add(
    agent_key: str = typer.Argument(..., help="The agent's stable key, for example customer-advisor."),
    file: Path = typer.Option(None, "--file", "-f", help="Definition JSON. Defaults to agents/<key>.json."),
    author: str = typer.Option("cli", "--author", help="Who created this version."),
    publish: bool = typer.Option(False, "--publish", help="Publish it straight away."),
    environment: str = typer.Option(None, "--promote", help="Also bind this environment to the new version."),
) -> None:
    """Add the next version of an agent as a draft."""
    path = file or Path("agents") / f"{agent_key}.json"
    config = _load_config(path)

    async def work() -> None:
        platform = _platform(require_prompts=False)
        try:
            definition = await platform.repository.create_draft(agent_key=agent_key, config=config, created_by=author)
            typer.echo(f"{agent_key} v{definition.version} created as draft")

            if publish or environment:
                await _publish(platform, agent_key, definition.version)
                typer.echo(f"{agent_key} v{definition.version} published")

            if environment:
                await platform.repository.bind(
                    agent_key=agent_key,
                    environment=environment,
                    version=definition.version,
                    updated_by=author,
                )
                typer.echo(f"{environment} now runs {agent_key} v{definition.version}")
        finally:
            await platform.close()

    _run(work())


async def _publish(platform, agent_key: str, version: int):
    """Publish, pinning a moving prompt label to the version that resolves now."""
    definition = await platform.repository.get(agent_key=agent_key, version=version)
    config = definition.config
    pinned = None

    if config.prompt.version is None and platform.prompts is not None:
        resolved = await platform.prompts.resolve(config.prompt)
        if resolved.version is not None:
            pinned = config.model_copy(deep=True)
            pinned.prompt.version = resolved.version
            pinned.prompt.label = None

    return await platform.repository.publish(agent_key=agent_key, version=version, pinned_config=pinned)


@agent_app.command("publish")
def agent_publish(
    agent_key: str = typer.Argument(...),
    version: int = typer.Argument(...),
) -> None:
    """Lock a draft. A published version never changes again."""

    async def work() -> None:
        platform = _platform(require_prompts=False)
        try:
            definition = await _publish(platform, agent_key, version)
            pinned = definition.config.prompt.version
            typer.echo(f"{agent_key} v{version} published" + (f", prompt pinned to v{pinned}" if pinned else ""))
        finally:
            await platform.close()

    _run(work())


@agent_app.command("promote")
def agent_promote(
    agent_key: str = typer.Argument(...),
    version: int = typer.Argument(...),
    environment: str = typer.Option("production", "--env"),
    author: str = typer.Option("cli", "--author"),
) -> None:
    """Point an environment at a published version. Rollback is the same command with an older version."""

    async def work() -> None:
        platform = _platform(require_prompts=False)
        try:
            await platform.repository.bind(
                agent_key=agent_key, environment=environment, version=version, updated_by=author
            )
            typer.echo(f"{environment} now runs {agent_key} v{version}")
        finally:
            await platform.close()

    _run(work())


@agent_app.command("list")
def agent_list(agent_key: str = typer.Argument(None)) -> None:
    """Show versions and which environment runs what."""

    async def work() -> None:
        platform = _platform(require_prompts=False)
        try:
            definitions = await platform.repository.list_versions(agent_key)
            rows = await platform.repository.bindings()
            bindings = {(b["agent_key"], b["agent_version"]): b["environment"] for b in rows}

            if not definitions:
                typer.echo("No agents yet. Try `asas-agent agent add <key>`.")
                return

            for definition in definitions:
                live = bindings.get((definition.agent_key, definition.version))
                marker = f"  ← {live}" if live else ""
                typer.echo(f"{definition.agent_key:<24} v{definition.version:<4} {definition.status:<10}{marker}")
        finally:
            await platform.close()

    _run(work())


@agent_app.command("show")
def agent_show(
    agent_key: str = typer.Argument(...),
    version: int = typer.Option(None, "--version"),
    environment: str = typer.Option(None, "--env"),
) -> None:
    """Print one definition as JSON."""

    async def work() -> None:
        platform = _platform(require_prompts=False)
        try:
            if environment:
                definition = await platform.repository.get_active(agent_key=agent_key, environment=environment)
            elif version:
                definition = await platform.repository.get(agent_key=agent_key, version=version)
            else:
                versions = await platform.repository.list_versions(agent_key)
                if not versions:
                    raise typer.BadParameter(f"{agent_key} has no versions")
                definition = versions[0]

            payload: dict[str, Any] = {
                "agent_key": definition.agent_key,
                "version": definition.version,
                "status": definition.status,
                "config": definition.config.model_dump(mode="json", by_alias=True),
            }
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        finally:
            await platform.close()

    _run(work())


@app.command()
def run(
    agent_key: str = typer.Argument(...),
    message: str = typer.Argument(..., help="What to ask the agent."),
    environment: str = typer.Option("production", "--env"),
    context_file: Path = typer.Option(None, "--context", help="JSON file of facts to pass in."),
    tenant: str = typer.Option("local", "--tenant"),
    user: str = typer.Option("cli", "--user"),
) -> None:
    """Run an agent once from the command line."""
    from asas_agent.runtime.context import RuntimeContext

    business_context = json.loads(context_file.read_text(encoding="utf-8")) if context_file else {}

    async def work() -> None:
        platform = _platform()
        try:
            result = await platform.runtime.run(
                agent_key=agent_key,
                environment=environment,
                user_input=message,
                business_context=business_context,
                context=RuntimeContext(
                    tenant_id=tenant,
                    user_id=user,
                    correlation_id=f"cli-{secrets.token_hex(4)}",
                    environment=environment,
                ),
            )
            output = result.output
            typer.echo(output.model_dump_json(indent=2) if hasattr(output, "model_dump_json") else str(output))
            typer.echo(
                f"\n{agent_key} v{result.agent_version}"
                + (f", prompt v{result.prompt_version}" if result.prompt_version else "")
                + (f", trace {result.trace_id}" if result.trace_id else "")
            )
        finally:
            await platform.close()

    _run(work())


@app.command()
def serve(
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the runtime API."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "asas_agent.api.app:app",
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
    )


if __name__ == "__main__":
    app()
