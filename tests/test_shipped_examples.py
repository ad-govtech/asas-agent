"""The definitions and the CLI a reader meets first.

Both are easy to leave behind when the schema changes: an example is not
imported by anything, and `serve` only runs when someone runs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from asas_agent.cli.main import app as cli_app
from asas_agent.registry.schema import AgentConfig

DEFINITIONS = sorted(Path("examples").rglob("*.json"))


def test_there_are_definitions_to_check():
    assert DEFINITIONS, "no example definitions found; this test would pass vacuously"


@pytest.mark.parametrize("path", DEFINITIONS, ids=lambda p: str(p))
def test_every_shipped_definition_still_loads(path):
    """A field removed from the schema must be removed from what ships with it."""
    AgentConfig.model_validate_json(path.read_text(encoding="utf-8"))


def test_serve_says_what_to_install_when_the_extra_is_missing(monkeypatch):
    """The Agents SDK brings uvicorn in for MCP, so its presence proves nothing."""
    import builtins

    real_import = builtins.__import__

    def without_fastapi(name, *args, **kwargs):
        if name == "fastapi":
            raise ImportError("No module named 'fastapi'", name="fastapi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fastapi)
    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code != 0
    assert "asas-agent[server]" in result.output
    assert "fastapi" in result.output
    assert "Traceback" not in result.output


def test_serve_also_reports_a_missing_uvicorn(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def without_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("No module named 'uvicorn'", name="uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_uvicorn)
    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code != 0
    assert "asas-agent[server]" in result.output
    assert "uvicorn" in result.output


def test_the_readme_does_not_document_an_api_that_was_removed():
    """A snippet that raises ImportError is worse than no snippet."""
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "ModelCapabilities" not in readme
    for name in ("sub_agents", "guardrails"):
        assert name not in json.dumps([json.loads(p.read_text()) for p in DEFINITIONS])
