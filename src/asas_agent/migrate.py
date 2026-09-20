"""Creating the registry tables from an application, not only from the CLI.

An application that embeds the runtime has to create these tables as part of its
own setup. Before this existed it had to copy the CLI's Alembic wiring, and find
out by traceback that the migration starts an event loop of its own and so
cannot run inside one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from asas_agent.config import Settings, get_settings


class MigrationError(RuntimeError):
    """Raised when the registry tables cannot be created."""


def migrate(settings: Settings | None = None, *, revision: str = "head") -> None:
    """Bring the registry schema up to date. Call it before an event loop starts.

        from asas_agent import migrate
        migrate()

    Safe to call again: a registry already at this revision is left alone.
    """
    from alembic import command
    from alembic.config import Config

    settings = settings or get_settings()
    if not settings.database_url:
        raise MigrationError("DATABASE_URL is not set, so there is no registry to migrate.")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise MigrationError(
            "migrate() runs its own event loop, so it cannot be called from inside one. "
            "Call it during start-up, before asyncio.run()."
        )

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    # Alembic reads this through ConfigParser, which treats `%` as interpolation,
    # so a percent-encoded password has to survive as itself.
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, revision)
