"""Configuration is refused before it can reach the runtime."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from asas_agent.registry.schema import AgentConfig

BASE = {
    "name": "Customer Advisor",
    "prompt": {"name": "agents/customer-advisor"},
    "model": {"provider": "openai", "name": "gpt-5.6-sol"},
}


def test_prompt_defaults_to_the_production_label():
    config = AgentConfig.model_validate(BASE)
    assert config.prompt.label == "production"
    assert config.prompt.version is None


def test_a_prompt_cannot_have_both_a_version_and_a_label():
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**BASE, "prompt": {"name": "p", "version": 3, "label": "production"}})


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**BASE, "run_this": "rm -rf /"})


def test_the_same_tool_cannot_be_listed_twice():
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**BASE, "tools": ["policy.search", "policy.search"]})


def test_two_sub_agents_cannot_share_a_tool_name():
    sub = {"agent_key": "a", "mode": "tool", "tool_name": "specialist"}
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**BASE, "sub_agents": [sub, {**sub, "agent_key": "b"}]})


def test_runtime_limits_are_capped():
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**BASE, "runtime": {"max_turns": 500}})


def test_output_schema_reads_from_the_json_name():
    config = AgentConfig.model_validate({**BASE, "output": {"schema": "customer_advice_v1"}})
    assert config.output.schema_key == "customer_advice_v1"
    assert config.model_dump(mode="json", by_alias=True)["output"]["schema"] == "customer_advice_v1"
