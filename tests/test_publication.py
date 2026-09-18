"""Repository publication freezes prompts for CLI and application callers alike."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from asas_agent.integrations.prompts import FilePrompts, PromptError
from asas_agent.registry.repository import AgentRepository, RegistryError
from asas_agent.registry.schema import AgentConfig


@pytest.fixture
def draft_session():
    config = AgentConfig(
        name="Advisor",
        prompt={"name": "advisor"},
        model={"provider": "openai", "name": "gpt-5-nano"},
    )
    row = SimpleNamespace(
        agent_key="advisor",
        version=1,
        status="draft",
        config=config.model_dump(mode="json", by_alias=True),
        created_at=datetime.now(UTC),
        created_by="test",
        published_at=None,
    )
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)

    async def execute(statement):
        if statement.is_select:
            return SimpleNamespace(one_or_none=lambda: row)
        # Stand in for Postgres RETURNING, using the actual SQL update values.
        values = statement.compile().params
        updated = SimpleNamespace(
            **{**vars(row), "config": values["config"], "status": values["status"], "published_at": datetime.now(UTC)}
        )
        return SimpleNamespace(one=lambda: updated)

    session.execute = AsyncMock(side_effect=execute)
    return row, session


async def test_repository_publishes_snapshot_before_source_file_changes(tmp_path, draft_session):
    row, session = draft_session
    path = tmp_path / "advisor.md"
    path.write_text("Original instructions", encoding="utf-8")
    provider = FilePrompts(tmp_path)
    repository = AgentRepository(lambda: session, prompts=provider)
    published = await repository.publish(agent_key="advisor", version=1)

    assert published.status == "published"
    assert published.config.prompt.snapshot == "Original instructions"
    assert row.config["prompt"]["snapshot"] is None
    path.write_text("New instructions", encoding="utf-8")
    assert (await provider.resolve(published.config.prompt)).text == "Original instructions"


async def test_repository_never_publishes_an_unresolved_prompt(tmp_path, draft_session):
    _, session = draft_session
    repository = AgentRepository(lambda: session, prompts=FilePrompts(tmp_path))
    with pytest.raises(PromptError, match="Cannot read"):
        await repository.publish(agent_key="advisor", version=1)
    assert session.execute.await_count == 1  # No UPDATE after failed resolution.


@pytest.mark.parametrize("status", ["published", "archived"])
async def test_repository_keeps_existing_versions_immutable(status, draft_session):
    row, session = draft_session
    row.status = status
    repository = AgentRepository(lambda: session)
    with pytest.raises(RegistryError, match=status):
        await repository.publish(agent_key="advisor", version=1)
    assert session.execute.await_count == 1
