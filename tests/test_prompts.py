"""Prompt choices work without optional services and freeze published behavior."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from asas_agent.bootstrap import _build_tracer, build_platform
from asas_agent.config import Settings
from asas_agent.integrations.prompts import (
    FilePrompts,
    LangfusePrompts,
    PromptError,
    build_prompt_provider,
    pin_prompt,
)
from asas_agent.registry.schema import PromptRef
from asas_agent.runtime.context import RuntimeContext


@pytest.fixture
def settings(monkeypatch):
    for field in Settings.model_fields.values():
        if field.alias:
            monkeypatch.delenv(field.alias, raising=False)
    return Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test", OPENAI_API_KEY="test")


async def test_default_platform_runs_without_langfuse(settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "langfuse", None)
    platform = build_platform(settings)
    try:
        assert isinstance(platform.prompts, FilePrompts)
        assert settings.tracing_provider == "none"
        from agents import Runner

        from asas_agent.registry.schema import AgentConfig

        config = AgentConfig(
            name="Test",
            prompt=PromptRef(name="missing-file", snapshot="Published instructions"),
            model={"provider": "openai", "name": "gpt-5-nano"},
        )
        monkeypatch.setattr(
            platform.repository, "get_active", AsyncMock(return_value=SimpleNamespace(config=config, version=2))
        )
        run = AsyncMock(return_value=SimpleNamespace(final_output="done"))
        monkeypatch.setattr(Runner, "run", run)
        result = await platform.runtime.run(
            agent_key="test",
            environment="dev",
            user_input="hello",
            context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        )
        assert result.output == "done"
        assert result.trace_id is None
        assert run.call_args.args[0].instructions == "Published instructions"
        assert run.call_args.kwargs["run_config"].tracing_disabled is True
    finally:
        await platform.close()


async def test_published_file_prompt_survives_edits_and_removal(tmp_path):
    path = tmp_path / "advisor.md"
    path.write_text("Hello {{name}}", encoding="utf-8")
    provider = FilePrompts(tmp_path)
    ref = PromptRef(name="advisor", variables={"name": "{{literal}}"})
    first = await pin_prompt(ref, provider)
    # The snapshot keeps its placeholders: a request's values are not known yet.
    assert first.snapshot == "Hello {{name}}"
    assert first.variables == {"name": "{{literal}}"}
    assert ref.snapshot is None

    path.write_text("Changed {{name}}", encoding="utf-8")
    second = await pin_prompt(ref, provider)
    path.unlink()

    # Round-trip through the JSON stored in Postgres, then resolve without files.
    restored = PromptRef.model_validate_json(first.model_dump_json())
    assert (await provider.resolve(restored)).text == "Hello {{literal}}"
    assert (await provider.resolve(second)).text == "Changed {{literal}}"
    assert (await pin_prompt(restored, None)).snapshot == first.snapshot
    # Rendering happens once, so a value that looks like a placeholder stays literal.
    assert (await LangfusePrompts().resolve(restored)).text == "Hello {{literal}}"


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside"])
async def test_file_prompt_cannot_escape_directory(tmp_path, name):
    with pytest.raises(PromptError, match="inside ASAS_PROMPT_DIR"):
        await FilePrompts(tmp_path).resolve(PromptRef(name=name))


async def test_file_prompt_cannot_follow_symlink_outside_directory(tmp_path):
    root = tmp_path / "prompts"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    with pytest.raises(PromptError, match="inside ASAS_PROMPT_DIR"):
        await FilePrompts(root).resolve(PromptRef(name="escape"))


@pytest.mark.parametrize("selector", [{"version": 2}, {"label": "staging"}])
async def test_file_provider_rejects_unsupported_selectors(tmp_path, selector):
    with pytest.raises(PromptError):
        await FilePrompts(tmp_path).resolve(PromptRef(name="advisor", **selector))


async def test_missing_prompt_or_provider_cannot_be_published(tmp_path):
    with pytest.raises(PromptError, match="Cannot read"):
        await pin_prompt(PromptRef(name="missing"), FilePrompts(tmp_path))
    with pytest.raises(PromptError, match="required to publish"):
        await pin_prompt(PromptRef(name="missing"), None)


@pytest.mark.parametrize("selector", [{"version": 1}, {"label": "production"}])
def test_snapshot_cannot_have_ambiguous_selectors(selector):
    with pytest.raises(ValidationError, match="snapshot"):
        PromptRef(name="advisor", snapshot="text", **selector)


async def test_langfuse_publication_pins_version_and_preserves_variables():
    client = MagicMock()
    client.get_prompt.return_value = SimpleNamespace(
        version=7, prompt="Hello {{name}}", compile=lambda **kw: f"Hello {kw['name']}"
    )
    provider = LangfusePrompts(client)
    pinned = await pin_prompt(PromptRef(name="advisor", variables={"name": "Ada"}), provider)
    client.get_prompt.assert_called_once_with("advisor", label="production")
    assert pinned.version == 7
    assert pinned.label is None
    assert pinned.snapshot is None
    assert (await provider.resolve(pinned)).text == "Hello Ada"
    client.get_prompt.assert_called_with("advisor", version=7)


def test_langfuse_missing_keys_and_optional_package_fail_clearly(settings, monkeypatch):
    settings.prompt_provider = "langfuse"
    with pytest.raises(PromptError, match="LANGFUSE_PUBLIC_KEY"):
        build_prompt_provider(settings)
    settings.langfuse_public_key = "pk-test"
    settings.langfuse_secret_key = "sk-test"
    monkeypatch.setitem(sys.modules, "langfuse", None)
    with pytest.raises(PromptError, match=r"asas-agent\[langfuse\]"):
        build_prompt_provider(settings)


def test_self_hosted_url_and_project_keys_reach_both_clients(settings, monkeypatch):
    client_type = MagicMock()
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=client_type))
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.openai_agents", None)
    from agents import set_trace_processors

    reset_processors = MagicMock(spec=set_trace_processors)
    monkeypatch.setattr("agents.set_trace_processors", reset_processors)
    settings.langfuse_host = "https://langfuse.internal.example"
    settings.langfuse_public_key = "pk-local"
    settings.langfuse_secret_key = "sk-local"
    settings.prompt_provider = "langfuse"
    provider = build_prompt_provider(settings)
    assert isinstance(provider, LangfusePrompts)
    assert _build_tracer(settings) is None  # Keys alone do not enable tracing.
    settings.tracing_provider = "langfuse"
    assert _build_tracer(settings) is client_type.return_value
    assert client_type.call_count == 2
    client_type.assert_called_with(
        public_key="pk-local", secret_key="sk-local", base_url="https://langfuse.internal.example"
    )
    reset_processors.assert_called_once_with([])  # No implicit OpenAI trace export.

    # File prompts can use Langfuse tracing, and the off switch still wins.
    settings.prompt_provider = "file"
    assert isinstance(build_prompt_provider(settings), FilePrompts)
    assert _build_tracer(settings) is client_type.return_value
    settings.tracing_enabled = False
    assert _build_tracer(settings) is None


def test_span_instrumentation_replaces_default_exporter(settings, monkeypatch):
    instrumentor = MagicMock()
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.openai_agents",
        SimpleNamespace(OpenAIAgentsInstrumentor=instrumentor),
    )
    monkeypatch.setattr("asas_agent.bootstrap.langfuse_client", MagicMock())
    settings.tracing_provider = "langfuse"
    _build_tracer(settings)
    instrumentor.return_value.instrument.assert_called_once_with(exclusive_processor=True)
