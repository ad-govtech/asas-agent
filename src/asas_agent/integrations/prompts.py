"""File or Langfuse prompts, with immutable references captured at publication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from asas_agent.config import Settings
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


def langfuse_client(settings: Settings):
    """Load the optional SDK only when the developer selects Langfuse."""
    if not settings.langfuse_configured:
        raise PromptError("Langfuse requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
    try:
        from langfuse import Langfuse
    except ImportError as exc:
        raise PromptError('Langfuse is optional. Install it with: uv pip install "asas-agent[langfuse]"') from exc
    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_host,
    )


async def pin_prompt(ref: PromptRef, provider: PromptProvider | None) -> PromptRef:
    """Freeze a provider version, or store rendered text when it has no versions."""
    if ref.snapshot is not None:
        return ref.model_copy(deep=True)
    if provider is None:
        raise PromptError("A working prompt provider is required to publish an agent")
    resolved = await provider.resolve(ref)
    if resolved.version is not None:
        return PromptRef(name=ref.name, version=resolved.version, variables=ref.variables)
    return PromptRef(name=ref.name, snapshot=resolved.text)


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
        if ref.snapshot is not None:
            return ResolvedPrompt(text=ref.snapshot, name=ref.name, version=None)
        return await asyncio.to_thread(self._resolve, ref)

    def _resolve(self, ref: PromptRef) -> ResolvedPrompt:
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
    """Reads drafts from a folder and published snapshots from the definition.

    `agents/customer-advisor` reads `<dir>/agents/customer-advisor.md`.
    The registry stores the rendered text at publication; no service is needed.
    """

    def __init__(self, directory: str | Path = "prompts"):
        self.directory = Path(directory)

    async def resolve(self, ref: PromptRef) -> ResolvedPrompt:
        if ref.snapshot is not None:
            return ResolvedPrompt(text=ref.snapshot, name=ref.name, version=None)
        if ref.version is not None:
            raise PromptError("File prompts use published snapshots, not numeric versions. Reference the file by name.")
        if ref.label not in (None, "production"):
            raise PromptError("File prompts do not support labels. Reference the file by name.")

        root = self.directory.resolve()
        path = (root / f"{ref.name}.md").resolve()
        if not path.is_relative_to(root):
            raise PromptError("Prompt files must stay inside ASAS_PROMPT_DIR")

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PromptError(f"Cannot read prompt file at {path}") from exc
        for key, value in ref.variables.items():
            text = text.replace("{{" + key + "}}", str(value))
        return ResolvedPrompt(text=text, name=ref.name, version=None)


def build_prompt_provider(settings: Settings) -> PromptProvider:
    if settings.prompt_provider == "file":
        return FilePrompts(settings.prompt_dir)
    return LangfusePrompts(client=langfuse_client(settings))
