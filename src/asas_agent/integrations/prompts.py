"""File or Langfuse prompts, with immutable references captured at publication.

A prompt is either a single block of text or a chat prompt: an ordered list of
role-tagged messages. Both kinds may carry `{{variables}}`. System messages
become the agent's instructions; the rest are sent as the first input messages
of the run, so a prompt author controls where each fact lands.

Variables are filled from the definition and from the values the caller passes
for this request. A variable with no value is an error, never a `{{name}}` left
in the text for the model to read.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from asas_agent.config import Settings
from asas_agent.registry.schema import PromptMessageRef, PromptRef

#: `{{ name }}`, the placeholder syntax Langfuse uses.
_VARIABLE = re.compile(r"{{\s*(\w+)\s*}}")

#: Roles a prompt may use. `system` and `developer` become instructions.
INSTRUCTION_ROLES = ("system", "developer")
MESSAGE_ROLES = ("user", "assistant")

Role = Literal["system", "developer", "user", "assistant"]


class PromptError(RuntimeError):
    """Raised when a prompt cannot be resolved."""


class PromptVariableError(PromptError):
    """Raised when a placeholder has no value. The caller can fix this; the prompt is fine."""


@dataclass(frozen=True)
class PromptMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class PromptTemplate:
    """A prompt as stored: messages that still contain their placeholders."""

    name: str
    version: int | None
    messages: tuple[PromptMessage, ...]
    is_chat: bool


@dataclass(frozen=True)
class ResolvedPrompt:
    """A prompt as run: instructions, and the messages that open the run."""

    name: str
    version: int | None
    instructions: str
    messages: tuple[PromptMessage, ...] = ()

    @property
    def text(self) -> str:
        """The instructions. Kept so callers that predate chat prompts still work."""
        return self.instructions


class PromptProvider(Protocol):
    async def template(self, ref: PromptRef) -> PromptTemplate: ...

    async def resolve(self, ref: PromptRef, variables: dict[str, Any] | None = None) -> ResolvedPrompt: ...


def _check_role(role: str, prompt_name: str) -> Role:
    if role not in INSTRUCTION_ROLES + MESSAGE_ROLES:
        raise PromptError(
            f"Prompt {prompt_name!r} uses the unsupported message role {role!r}. "
            f"Use one of: {', '.join(INSTRUCTION_ROLES + MESSAGE_ROLES)}"
        )
    return role  # type: ignore[return-value]


def _as_messages(compiled: Any, prompt_name: str) -> tuple[tuple[PromptMessage, ...], bool]:
    """Normalize what a provider returns into messages, and say whether it was a chat prompt."""
    if isinstance(compiled, str):
        return (PromptMessage(role="system", content=compiled),), False

    messages: list[PromptMessage] = []
    for message in compiled:
        if isinstance(message, PromptMessage):
            messages.append(message)
            continue
        if not isinstance(message, dict) or "role" not in message or "content" not in message:
            # Langfuse chat placeholders arrive as {"type": "placeholder", ...}.
            raise PromptError(
                f"Prompt {prompt_name!r} contains a message this runtime cannot use: {message!r}. "
                "Chat placeholders are not supported; use variables instead."
            )
        role = _check_role(str(message["role"]), prompt_name)
        messages.append(PromptMessage(role=role, content=str(message["content"])))

    if not messages:
        raise PromptError(f"Prompt {prompt_name!r} has no messages")
    return tuple(messages), True


def render(template: PromptTemplate, variables: dict[str, Any]) -> ResolvedPrompt:
    """Fill placeholders, then split instructions from the opening messages."""
    rendered = tuple(
        PromptMessage(role=message.role, content=_fill(message.content, variables, template.name))
        for message in template.messages
    )
    return _split(template, rendered)


def _fill(text: str, variables: dict[str, Any], prompt_name: str) -> str:
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return str(variables[name])
        missing.append(name)
        return match.group(0)

    filled = _VARIABLE.sub(substitute, text)
    if missing:
        raise PromptVariableError(
            f"Prompt {prompt_name!r} needs values for: {', '.join(sorted(set(missing)))}. "
            "Pass them as prompt variables, in the definition or with the request."
        )
    return filled


def _split(template: PromptTemplate, messages: tuple[PromptMessage, ...]) -> ResolvedPrompt:
    """System and developer messages become instructions; user and assistant messages open the run."""
    instructions = "\n\n".join(m.content for m in messages if m.role in INSTRUCTION_ROLES)
    opening = tuple(m for m in messages if m.role in MESSAGE_ROLES)

    if not instructions:
        raise PromptError(f"Prompt {template.name!r} has no system message, so the agent would have no instructions.")
    return ResolvedPrompt(
        name=template.name,
        version=template.version,
        instructions=instructions,
        messages=opening,
    )


def _guard_rendered(messages: tuple[PromptMessage, ...], prompt_name: str) -> None:
    """A provider that renders for us still must not leave a placeholder behind."""
    missing = sorted({name for message in messages for name in _VARIABLE.findall(message.content)})
    if missing:
        raise PromptVariableError(
            f"Prompt {prompt_name!r} needs values for: {', '.join(missing)}. "
            "Pass them as prompt variables, in the definition or with the request."
        )


def _snapshot_template(ref: PromptRef) -> PromptTemplate | None:
    """The template a published definition carries, if it carries one."""
    if ref.snapshot_messages is not None:
        messages = tuple(PromptMessage(role=m.role, content=m.content) for m in ref.snapshot_messages)
        return PromptTemplate(name=ref.name, version=None, messages=messages, is_chat=True)
    if ref.snapshot is not None:
        return PromptTemplate(
            name=ref.name,
            version=None,
            messages=(PromptMessage(role="system", content=ref.snapshot),),
            is_chat=False,
        )
    return None


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
    """Freeze a provider version, or store the template itself when it has no versions.

    The stored template still carries its placeholders: values that belong to a
    request cannot be known at publication. The definition is immutable, so the
    variables it sets are frozen with it.
    """
    if ref.snapshot is not None or ref.snapshot_messages is not None:
        return ref.model_copy(deep=True)
    if provider is None:
        raise PromptError("A working prompt provider is required to publish an agent")

    template = await provider.template(ref)
    if template.version is not None:
        return PromptRef(name=ref.name, version=template.version, variables=ref.variables)
    if template.is_chat:
        return PromptRef(
            name=ref.name,
            snapshot_messages=[PromptMessageRef(role=m.role, content=m.content) for m in template.messages],
            variables=ref.variables,
        )
    return PromptRef(name=ref.name, snapshot=template.messages[0].content, variables=ref.variables)


class LangfusePrompts:
    """Reads prompts from Langfuse by version, or by label such as `production`."""

    def __init__(self, client=None):
        self._client = client

    def _get_client(self):
        if self._client is None:
            from langfuse import get_client

            self._client = get_client()
        return self._client

    async def template(self, ref: PromptRef) -> PromptTemplate:
        snapshot = _snapshot_template(ref)
        if snapshot is not None:
            return snapshot
        return await asyncio.to_thread(self._template, ref)

    def _template(self, ref: PromptRef) -> PromptTemplate:
        prompt = self._fetch(ref)
        messages, is_chat = _as_messages(prompt.prompt, ref.name)
        return PromptTemplate(
            name=ref.name,
            version=getattr(prompt, "version", None),
            messages=messages,
            is_chat=is_chat,
        )

    async def resolve(self, ref: PromptRef, variables: dict[str, Any] | None = None) -> ResolvedPrompt:
        values = {**ref.variables, **(variables or {})}
        snapshot = _snapshot_template(ref)
        if snapshot is not None:
            return render(snapshot, values)
        return await asyncio.to_thread(self._resolve, ref, values)

    def _resolve(self, ref: PromptRef, values: dict[str, Any]) -> ResolvedPrompt:
        prompt = self._fetch(ref)
        # Langfuse renders its own templates, so the text matches what its UI shows.
        compiled = prompt.compile(**values) if values else prompt.compile()
        messages, is_chat = _as_messages(compiled, ref.name)
        _guard_rendered(messages, ref.name)
        template = PromptTemplate(
            name=ref.name,
            version=getattr(prompt, "version", None),
            messages=messages,
            is_chat=is_chat,
        )
        return _split(template, messages)

    def _fetch(self, ref: PromptRef):
        client = self._get_client()
        try:
            if ref.version is not None:
                return client.get_prompt(ref.name, version=ref.version)
            return client.get_prompt(ref.name, label=ref.label or "production")
        except Exception as exc:  # noqa: BLE001 - surfaced as a runtime error with context
            selector = f"version {ref.version}" if ref.version is not None else f"label {ref.label or 'production'}"
            raise PromptError(f"Langfuse has no prompt {ref.name!r} at {selector}") from exc


class FilePrompts:
    """Reads drafts from a folder and published snapshots from the definition.

    `agents/customer-advisor` reads `<dir>/agents/customer-advisor.md` as a text
    prompt, or `<dir>/agents/customer-advisor.chat.json` as a chat prompt: a
    list of `{"role": ..., "content": ...}` messages.

    The registry stores the template at publication; no service is needed.
    """

    def __init__(self, directory: str | Path = "prompts"):
        self.directory = Path(directory)

    async def template(self, ref: PromptRef) -> PromptTemplate:
        snapshot = _snapshot_template(ref)
        if snapshot is not None:
            return snapshot

        if ref.version is not None:
            raise PromptError("File prompts use published snapshots, not numeric versions. Reference the file by name.")
        if ref.label not in (None, "production"):
            raise PromptError("File prompts do not support labels. Reference the file by name.")

        chat_path = self._path(f"{ref.name}.chat.json")
        if chat_path.exists():
            return PromptTemplate(
                name=ref.name,
                version=None,
                messages=_as_messages(self._read_chat(chat_path, ref.name), ref.name)[0],
                is_chat=True,
            )
        return PromptTemplate(
            name=ref.name,
            version=None,
            messages=(PromptMessage(role="system", content=self._read(self._path(f"{ref.name}.md"))),),
            is_chat=False,
        )

    async def resolve(self, ref: PromptRef, variables: dict[str, Any] | None = None) -> ResolvedPrompt:
        template = await self.template(ref)
        return render(template, {**ref.variables, **(variables or {})})

    def _path(self, relative: str) -> Path:
        root = self.directory.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise PromptError("Prompt files must stay inside ASAS_PROMPT_DIR")
        return path

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PromptError(f"Cannot read prompt file at {path}") from exc

    def _read_chat(self, path: Path, prompt_name: str) -> Any:
        import json

        try:
            messages = json.loads(self._read(path))
        except json.JSONDecodeError as exc:
            raise PromptError(f"Prompt file {path} is not valid JSON") from exc
        if not isinstance(messages, list):
            raise PromptError(f"Prompt file {path} must hold a list of messages")
        return messages


def build_prompt_provider(settings: Settings) -> PromptProvider:
    if settings.prompt_provider == "file":
        return FilePrompts(settings.prompt_dir)
    return LangfusePrompts(client=langfuse_client(settings))
