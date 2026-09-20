"""The agent definition: what an agent is, as data.

Configuration chooses capabilities. It never carries implementation: no code,
no SQL, no URLs, no secrets. The runtime resolves every name through a registry.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

Status = Literal["draft", "published", "archived"]
Environment = Literal["dev", "test", "staging", "production"]


class PromptMessageRef(BaseModel):
    """One message of a chat prompt, as published."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user"]
    content: str


class PromptRef(BaseModel):
    """Where the instructions live. A version pins it; a label follows a moving target.

    A prompt with no versions of its own is published as a snapshot: `snapshot`
    for a text prompt, `snapshot_messages` for a chat prompt. A snapshot keeps
    its `{{placeholders}}`, because the values that fill them belong to a
    request; the variables the definition sets are frozen with the definition.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int | None = None
    label: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    snapshot: str | None = None
    snapshot_messages: list[PromptMessageRef] | None = None

    @model_validator(mode="after")
    def _one_selector(self) -> PromptRef:
        if self.snapshot is not None and self.snapshot_messages is not None:
            raise ValueError("Set either a text snapshot or chat snapshot messages, not both")
        if self.snapshot is not None or self.snapshot_messages is not None:
            if self.version is not None or self.label is not None:
                raise ValueError("A prompt snapshot cannot also have a version or a label")
            if self.snapshot_messages is not None and not self.snapshot_messages:
                raise ValueError("A chat prompt snapshot needs at least one message")
            return self
        if self.version is None and self.label is None:
            self.label = "production"
        if self.version is not None and self.label is not None:
            raise ValueError("Set either a prompt version or a label, not both")
        return self


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    name: str
    settings: dict[str, Any] = Field(default_factory=dict)


class RuntimeLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_turns: int = Field(default=8, ge=1, le=50)
    timeout_seconds: int = Field(default=45, ge=1, le=300)


class OutputConfig(BaseModel):
    """`schema` in JSON, `schema_key` in Python, because `schema` is taken by Pydantic."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_key: str | None = Field(default=None, alias="schema")


class AgentConfig(BaseModel):
    """One agent version. Everything that changes behavior lives here."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    name: str
    description: str | None = None

    prompt: PromptRef
    model: ModelRef

    tools: list[str] = Field(default_factory=list)

    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _no_duplicate_tools(self) -> AgentConfig:
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("The same tool is listed twice")
        return self
