"""Capabilities, output schemas and the caller policy."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from asas_agent.config import normalize_dsn
from asas_agent.registry.capabilities import CapabilityError, CapabilityRegistry
from asas_agent.registry.outputs import OutputSchemaError, OutputSchemaRegistry
from asas_agent.runtime.context import CapabilityPolicy, RuntimeContext


def context(**kwargs) -> RuntimeContext:
    return RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1", **kwargs)


def test_an_unknown_capability_is_refused():
    registry = CapabilityRegistry()
    with pytest.raises(CapabilityError, match="Unknown capability"):
        registry.resolve_many(["policy.search"], context())


def test_a_registered_capability_is_built_with_the_request_context():
    registry = CapabilityRegistry()
    seen = {}

    @registry.capability("policy.search")
    def make(ctx):
        seen["tenant"] = ctx.tenant_id
        return "tool"

    assert registry.resolve_many(["policy.search"], context()) == ["tool"]
    assert seen["tenant"] == "T1"


def test_action_tools_need_to_be_enabled_for_the_caller():
    registry = CapabilityRegistry()
    registry.capability("case.create", risk="action")(lambda ctx: "tool")

    with pytest.raises(CapabilityError, match="changes data"):
        registry.resolve_many(["case.create"], context())

    allowed = context(policy=CapabilityPolicy(allow_action_tools=True))
    assert registry.resolve_many(["case.create"], allowed) == ["tool"]


def test_a_caller_can_be_limited_to_fewer_tools_than_the_definition_lists():
    registry = CapabilityRegistry()
    registry.capability("policy.search")(lambda ctx: "tool")

    limited = context(policy=CapabilityPolicy(allowed={"case.history"}))
    with pytest.raises(CapabilityError, match="not allowed for this caller"):
        registry.resolve_many(["policy.search"], limited)


def test_output_schemas_resolve_by_name():
    registry = OutputSchemaRegistry()

    @registry.schema("advice_v1")
    class Advice(BaseModel):
        explanation: str

    assert registry.resolve("advice_v1") is Advice
    assert registry.resolve(None) is None
    with pytest.raises(OutputSchemaError):
        registry.resolve("missing_v1")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        ("postgresql://u@host:5432/db", "postgresql+asyncpg://u@host:5432/db"),
        ("postgresql://u@host/db?sslmode=require", "postgresql+asyncpg://u@host/db?ssl=require"),
        ("postgresql+asyncpg://u@host/db", "postgresql+asyncpg://u@host/db"),
    ],
)
def test_connection_strings_are_normalized_for_asyncpg(given, expected):
    assert normalize_dsn(given) == expected
