"""Creating the registry tables from an application, not only from the CLI."""

from __future__ import annotations

import asyncio

import pytest

from asas_agent import migrate
from asas_agent.config import Settings
from asas_agent.migrate import MigrationError


@pytest.fixture
def settings(monkeypatch):
    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    return Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test")


def test_it_is_exported_for_an_application_to_call():
    import asas_agent

    assert "migrate" in asas_agent.__all__


def test_a_registry_with_no_url_says_so(settings):
    settings.database_url = ""
    with pytest.raises(MigrationError, match="DATABASE_URL"):
        migrate(settings)


async def test_calling_it_inside_an_event_loop_says_what_to_do_instead(settings):
    """It runs its own loop; an async application would otherwise get a RuntimeError from Alembic."""
    with pytest.raises(MigrationError, match="before asyncio.run"):
        migrate(settings)


def test_a_percent_encoded_password_survives(settings, monkeypatch):
    from unittest.mock import MagicMock

    settings.database_url = "postgresql+asyncpg://u:pa%40ss%25@host/db"
    upgrade = MagicMock()
    monkeypatch.setattr("alembic.command.upgrade", upgrade)

    migrate(settings)

    assert upgrade.call_args.args[0].get_main_option("sqlalchemy.url") == settings.database_url
    assert upgrade.call_args.args[1] == "head"


def test_the_loop_check_does_not_fire_outside_one(settings, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr("alembic.command.upgrade", MagicMock())
    migrate(settings)  # No exception: there is no running loop here.
    assert asyncio.get_event_loop_policy() is not None
