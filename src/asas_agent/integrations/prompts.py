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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from asas_agent.config import Settings
from asas_agent.registry.schema import PromptMessageRef, PromptRef

#: `{{ name }}`, the placeholder syntax Langfuse uses.
_OPENING, _CLOSING = "{{", "}}"

#: Roles a prompt may use. `system` and `developer` become instructions.
INSTRUCTION_ROLES = ("system", "developer")
MESSAGE_ROLES = ("user", "assistant")

Role = Literal["system", "developer", "user", "assistant"]


class PromptError(RuntimeError):
    """Raised when a prompt cannot be resolved."""


class PromptVariableError(PromptError):
    """Raised when a placeholder has no value. The caller can fix this; the prompt is fine."""


class PromptShapeError(PromptError):
    """Raised when a prompt cannot instruct an agent. Configuration is wrong, not the service."""


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
    #: The placeholder names this prompt required, for the trace. Not their values.
    filled: tuple[str, ...] = ()

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
        content = message["content"]
        if not isinstance(content, str):
            raise PromptError(
                f"Prompt {prompt_name!r} has a {role} message whose content is {type(content).__name__}, not text. "
                "This runtime sends text; write the message as a string."
            )
        messages.append(PromptMessage(role=role, content=content))

    if not messages:
        raise PromptError(f"Prompt {prompt_name!r} has no messages")
    return tuple(messages), True


def _next_variable(content: str, start: int) -> tuple[str, int, int] | None:
    """Langfuse's rule: find `{{`, take the next `}}`, strip what is between."""
    opened = content.find(_OPENING, start)
    if opened == -1:
        return None
    closed = content.find(_CLOSING, opened)
    if closed == -1:
        return None
    return content[opened + len(_OPENING) : closed].strip(), opened, closed + len(_CLOSING)


def variable_names(content: str) -> list[str]:
    names, cursor = [], 0
    while cursor < len(content):
        found = _next_variable(content, cursor)
        if not found:
            break
        names.append(found[0])
        cursor = found[2]
    return names


def _fill(content: str, variables: dict[str, Any]) -> str:
    """Substitute once, left to right. A value is never scanned for placeholders of its own."""
    out, cursor = [], 0
    while cursor < len(content):
        found = _next_variable(content, cursor)
        if not found:
            out.append(content[cursor:])
            break
        name, opened, closed = found
        out.append(content[cursor:opened])
        if name in variables:
            value = variables[name]
            # `None` renders as nothing, and every other value through `str()`,
            # which is what Langfuse does. A prompt reads the same either way.
            out.append("" if value is None else str(value))
        else:
            out.append(content[opened:closed])
        cursor = closed
    return "".join(out)


def render(template: PromptTemplate, variables: dict[str, Any]) -> ResolvedPrompt:
    """Fill every placeholder, then split instructions from the opening messages."""
    required = {name for message in template.messages for name in variable_names(message.content)}
    missing = sorted(required - set(variables))
    if missing:
        raise PromptVariableError(
            f"Prompt {template.name!r} needs values for: {', '.join(missing)}. "
            "Pass them as prompt variables, in the definition or with the request."
        )

    rendered = tuple(
        PromptMessage(role=message.role, content=_fill(message.content, variables)) for message in template.messages
    )
    return _split(template, rendered, filled=tuple(sorted(required)))


def _split(
    template: PromptTemplate, messages: tuple[PromptMessage, ...], filled: tuple[str, ...] = ()
) -> ResolvedPrompt:
    """System and developer messages instruct the agent; user and assistant messages open the run."""
    instructions = "\n\n".join(m.content for m in messages if m.role in INSTRUCTION_ROLES and m.content.strip())
    opening = tuple(m for m in messages if m.role in MESSAGE_ROLES)

    if not any(m.role in INSTRUCTION_ROLES for m in messages):
        raise PromptShapeError(
            f"Prompt {template.name!r} has no system message, so the agent would have no instructions."
        )
    if not instructions:
        raise PromptShapeError(f"Prompt {template.name!r} has a system message, but it is empty.")

    first_message = next((i for i, m in enumerate(messages) if m.role in MESSAGE_ROLES), len(messages))
    trailing = [m.role for m in messages[first_message:] if m.role in INSTRUCTION_ROLES]
    if trailing:
        raise PromptShapeError(
            f"Prompt {template.name!r} puts a {trailing[0]} message after a user or assistant message. "
            "Instructions are hoisted out of the conversation, so write them first."
        )

    return ResolvedPrompt(
        name=template.name,
        version=template.version,
        instructions=instructions,
        messages=opening,
        filled=filled,
    )


def merge_variables(
    ref: PromptRef, variables: dict[str, Any] | None, template: PromptTemplate | None = None
) -> dict[str, Any]:
    """The definition's values, plus this request's.

    What a request may fill is not configured anywhere: it is what the template
    asks for and the definition has not already answered. Anything else would
    not reach the model, so sending it is a mistake worth reporting.

    A request may not replace a value the definition sets. Those are part of an
    immutable version and they reach the system instructions, so a caller able
    to rewrite one could rewrite a published agent's instructions.
    """
    request = variables or {}

    frozen = sorted(set(ref.variables) & set(request))
    if frozen:
        raise PromptVariableError(
            f"Prompt {ref.name!r} already sets {', '.join(frozen)} in the published definition. "
            "Publish a new version to change it; a request cannot."
        )

    if template is not None:
        wanted = {name for message in template.messages for name in variable_names(message.content)}
        unused = sorted(set(request) - wanted)
        if unused:
            accepted = sorted(wanted - set(ref.variables))
            raise PromptVariableError(
                f"Prompt {ref.name!r} does not use {', '.join(unused)}. "
                f"It asks for: {', '.join(accepted) or 'nothing from a request'}."
            )

    return {**ref.variables, **request}


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
    variables it sets, and the names it lets a request fill, are frozen with it.
    """
    if ref.snapshot is not None or ref.snapshot_messages is not None:
        return ref.model_copy(deep=True)
    if provider is None:
        raise PromptError("A working prompt provider is required to publish an agent")

    template = await provider.template(ref)
    # Substitution is not possible yet, but the shape is already decidable.
    _split(template, template.messages)
    frozen = {"variables": ref.variables}

    if template.version is not None:
        return PromptRef(name=ref.name, version=template.version, **frozen)
    if template.is_chat:
        return PromptRef(
            name=ref.name,
            snapshot_messages=[PromptMessageRef(role=m.role, content=m.content) for m in template.messages],
            **frozen,
        )
    return PromptRef(name=ref.name, snapshot=template.messages[0].content, **frozen)


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
        # Rendering is ours, not the SDK's: `compile()` cannot take a variable
        # named `self`, and this way a file prompt and a Langfuse prompt with
        # the same text render identically.
        template = await self.template(ref)
        return render(template, merge_variables(ref, variables, template))

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

    def __init__(self, directory: str | Path = "prompts", *, cache: bool = True):
        self.directory = Path(directory)
        #: Parsed templates, keyed by what the file looked like when they were read.
        self._cache: dict[str, tuple[tuple[Any, ...], PromptTemplate]] | None = {} if cache else None

    async def template(self, ref: PromptRef) -> PromptTemplate:
        snapshot = _snapshot_template(ref)
        if snapshot is not None:
            return snapshot

        if ref.version is not None:
            raise PromptError("File prompts use published snapshots, not numeric versions. Reference the file by name.")
        if ref.label not in (None, "production"):
            raise PromptError("File prompts do not support labels. Reference the file by name.")

        return await asyncio.to_thread(self._template, ref)

    def _template(self, ref: PromptRef) -> PromptTemplate:
        chat_path = self._path(f"{ref.name}.chat.json")
        try:
            is_chat = chat_path.exists()
        except OSError as exc:
            raise PromptError(f"Cannot read prompt file at {chat_path}") from exc

        path = chat_path if is_chat else self._path(f"{ref.name}.md")
        # Stamp before reading: a file written while we read it must not be
        # cached under the stamp of the version we did not get.
        stamp = self._stamp(path)
        cached = self._cached(ref.name, stamp)
        if cached is not None:
            return cached

        if is_chat:
            template = PromptTemplate(
                name=ref.name,
                version=None,
                messages=_as_messages(self._read_chat(path, ref.name), ref.name)[0],
                is_chat=True,
            )
        else:
            template = PromptTemplate(
                name=ref.name,
                version=None,
                messages=(PromptMessage(role="system", content=self._read(path)),),
                is_chat=False,
            )
        self._remember(ref.name, stamp, path, template)
        return template

    def _stamp(self, path: Path) -> tuple[Any, ...] | None:
        """What the file is right now. An edit changes it, so an edit is picked up.

        A replacement that preserves the original timestamp and size - `cp -p`,
        `rsync -a`, restoring a backup - looks unchanged, so restart the
        process after one, or turn the cache off.
        """
        try:
            status = path.stat()
        except OSError:
            return None
        return (str(path), status.st_mtime_ns, status.st_size)

    def _cached(self, name: str, stamp: tuple[Any, ...] | None) -> PromptTemplate | None:
        if self._cache is None or stamp is None:
            return None
        entry = self._cache.get(name)
        if entry is None:
            return None
        cached_stamp, template = entry
        return template if cached_stamp == stamp else None

    def _remember(self, name: str, stamp: tuple[Any, ...] | None, path: Path, template: PromptTemplate) -> None:
        if self._cache is None or stamp is None:
            return
        if self._stamp(path) != stamp:
            return  # The file changed while we read it; read it again next time.
        self._cache[name] = (stamp, template)

    async def resolve(self, ref: PromptRef, variables: dict[str, Any] | None = None) -> ResolvedPrompt:
        template = await self.template(ref)
        return render(template, merge_variables(ref, variables, template))

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
