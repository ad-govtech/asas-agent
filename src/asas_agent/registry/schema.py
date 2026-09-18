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


class PromptRef(BaseModel):
    """Where the instructions live. A version pins it; a label follows a moving target."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int | None = None
    label: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    snapshot: str | None = None

    @model_validator(mode="after")
    def _one_selector(self) -> PromptRef:
        if self.snapshot is not None:
            if self.version is not None or self.label is not None or self.variables:
                raise ValueError("A rendered prompt snapshot cannot have a version, label, or variables")
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


class SubAgentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str
    environment: Environment = "production"
    mode: Literal["tool", "handoff"]
    tool_name: str | None = None
    description: str | None = None


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
    sub_agents: list[SubAgentRef] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)

    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _no_duplicate_tools(self) -> AgentConfig:
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("The same tool is listed twice")
        names = [s.tool_name or s.agent_key for s in self.sub_agents if s.mode == "tool"]
        if len(set(names)) != len(names):
            raise ValueError("Two sub-agents would be exposed under the same tool name")
        return self
