"""Prompt resolution.

Langfuse holds prompt text and versions. The runtime records the exact version
it ran, so a trace can be reproduced. A missing production prompt is an error,
never a silent fall back to the latest draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from asas_agent.registry.schema import PromptRef


class PromptError(RuntimeError):
    """Raised when a prompt cannot be resolved."""


@dataclass(frozen=True)
class ResolvedPrompt:
    text: str
    name: str
    version: int | None


class PromptProvider(Protocol):
    async def resolve(self, ref: PromptRef) -> ResolvedPrompt: ...


class LangfusePrompts:
    """Reads prompts from Langfuse by version, or by label such as `production`."""

    def __init__(self, client=None):
        self._client = client

    def _get_client(self):
        if self._client is None:
            from langfuse import get_client

            self._client = get_client()
        return self._client

    async def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        client = self._get_client()
        try:
            if ref.version is not None:
                prompt = client.get_prompt(ref.name, version=ref.version)
            else:
                prompt = client.get_prompt(ref.name, label=ref.label or "production")
        except Exception as exc:  # noqa: BLE001 - surfaced as a runtime error with context
            selector = f"version {ref.version}" if ref.version is not None else f"label {ref.label or 'production'}"
            raise PromptError(f"Langfuse has no prompt {ref.name!r} at {selector}") from exc

        text = prompt.compile(**ref.variables) if ref.variables else prompt.compile()
        return ResolvedPrompt(text=text, name=ref.name, version=getattr(prompt, "version", None))


class FilePrompts:
    """Reads prompts from a folder. For local work before Langfuse is connected.

    `agents/customer-advisor` reads `<dir>/agents/customer-advisor.md`.
    Versions do not exist here, so a definition that pins a version is refused.
    """

    def __init__(self, directory: str | Path = "prompts"):
        self.directory = Path(directory)

    async def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        if ref.version is not None:
            raise PromptError("File prompts have no versions. Use Langfuse, or reference the prompt by name only.")

        path = self.directory / f"{ref.name}.md"
        if not path.exists():
            raise PromptError(f"No prompt file at {path}")

        text = path.read_text(encoding="utf-8")
        for key, value in ref.variables.items():
            text = text.replace("{{" + key + "}}", str(value))
        return ResolvedPrompt(text=text, name=ref.name, version=None)


def build_prompt_provider(settings) -> PromptProvider:
    if settings.prompt_provider == "file":
        return FilePrompts(settings.prompt_dir)
    if not settings.langfuse_configured:
        raise PromptError(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set. "
            "Set them, or use ASAS_PROMPT_PROVIDER=file for local work."
        )
    return LangfusePrompts()
