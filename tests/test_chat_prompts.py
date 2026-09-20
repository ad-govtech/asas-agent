"""Chat prompts and request-time variables.

A chat prompt keeps its shape: system messages instruct the agent, and the
remaining messages open the run. Values that belong to a request are passed
with the request, never frozen into a published definition.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from asas_agent.integrations.prompts import (
    FilePrompts,
    LangfusePrompts,
    PromptError,
    PromptMessage,
    PromptTemplate,
    PromptVariableError,
    pin_prompt,
    render,
)
from asas_agent.registry.schema import AgentConfig, PromptRef
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.factory import AgentFactory
from asas_agent.runtime.runner import AgentRuntime, _run_input

CHAT = [
    {"role": "system", "content": "You score {{area}}."},
    {"role": "user", "content": "Profile: {{candidate_profile}}"},
]


def chat_client(messages=None, version=7):
    """A Langfuse client returning a chat prompt, rendered the way Langfuse renders."""
    raw = messages if messages is not None else CHAT

    def compile(**kwargs):
        rendered = []
        for message in raw:
            if "content" not in message:  # A chat placeholder passes through untouched.
                rendered.append(message)
                continue
            content = message["content"]
            for key, value in kwargs.items():
                content = content.replace("{{" + key + "}}", str(value))
            rendered.append({"role": message["role"], "content": content})
        return rendered

    client = MagicMock()
    client.get_prompt.return_value = SimpleNamespace(version=version, prompt=raw, compile=compile)
    return client


def file_prompts(tmp_path, messages=None):
    (tmp_path / "scorer.chat.json").write_text(json.dumps(messages if messages is not None else CHAT))
    return FilePrompts(tmp_path)


async def test_a_chat_prompt_splits_into_instructions_and_opening_messages():
    provider = LangfusePrompts(chat_client())
    resolved = await provider.resolve(PromptRef(name="scorer"), {"area": "Delivery", "candidate_profile": {"yrs": 7}})
    assert resolved.instructions == "You score Delivery."
    assert resolved.messages == (PromptMessage(role="user", content="Profile: {'yrs': 7}"),)
    assert resolved.version == 7


async def test_several_system_messages_join_in_order():
    provider = LangfusePrompts(
        chat_client(
            [
                {"role": "system", "content": "First."},
                {"role": "developer", "content": "Second."},
                {"role": "user", "content": "Go."},
            ]
        )
    )
    resolved = await provider.resolve(PromptRef(name="scorer"))
    assert resolved.instructions == "First.\n\nSecond."
    assert [m.role for m in resolved.messages] == ["user"]


async def test_a_text_prompt_still_becomes_instructions_with_no_messages(tmp_path):
    (tmp_path / "advisor.md").write_text("Only instructions", encoding="utf-8")
    resolved = await FilePrompts(tmp_path).resolve(PromptRef(name="advisor"))
    assert resolved.instructions == "Only instructions"
    assert resolved.messages == ()


async def test_a_chat_prompt_without_a_system_message_is_refused():
    provider = LangfusePrompts(chat_client([{"role": "user", "content": "Just do it"}]))
    with pytest.raises(PromptError, match="no system message"):
        await provider.resolve(PromptRef(name="scorer"))


async def test_an_unsupported_role_or_placeholder_message_is_refused():
    provider = LangfusePrompts(chat_client([{"role": "tool", "content": "x"}]))
    with pytest.raises(PromptError, match="unsupported message role"):
        await provider.resolve(PromptRef(name="scorer"))

    provider = LangfusePrompts(chat_client([{"type": "placeholder", "name": "history"}]))
    with pytest.raises(PromptError, match="cannot use"):
        await provider.resolve(PromptRef(name="scorer"))


async def test_request_variables_add_to_the_definitions_own():
    provider = LangfusePrompts(chat_client())
    resolved = await provider.resolve(
        PromptRef(name="scorer", variables={"area": "Delivery"}), {"candidate_profile": "Ada"}
    )
    assert resolved.instructions == "You score Delivery."
    assert resolved.messages[0].content == "Profile: Ada"


@pytest.mark.parametrize("provider_name", ["langfuse", "file"])
async def test_a_request_cannot_replace_a_value_the_published_definition_sets(tmp_path, provider_name):
    """Definition variables reach the system instructions, so a caller must not rewrite them."""
    provider = LangfusePrompts(chat_client()) if provider_name == "langfuse" else file_prompts(tmp_path)
    ref = PromptRef(name="scorer", variables={"area": "Delivery"})
    with pytest.raises(PromptVariableError, match="already sets area"):
        await provider.resolve(ref, {"area": "Anything", "candidate_profile": "Ada"})


@pytest.mark.parametrize("provider_name", ["langfuse", "file"])
async def test_a_variable_with_no_value_is_an_error_not_a_placeholder_sent_to_the_model(tmp_path, provider_name):
    provider = LangfusePrompts(chat_client()) if provider_name == "langfuse" else file_prompts(tmp_path)
    with pytest.raises(PromptVariableError, match="candidate_profile"):
        await provider.resolve(PromptRef(name="scorer"), {"area": "Delivery"})


async def test_spaces_inside_a_placeholder_are_accepted(tmp_path):
    provider = file_prompts(tmp_path, [{"role": "system", "content": "You score {{ area }}."}])
    resolved = await provider.resolve(PromptRef(name="scorer"), {"area": "Delivery"})
    assert resolved.instructions == "You score Delivery."


@pytest.mark.parametrize("provider_name", ["langfuse", "file"])
async def test_a_filled_value_that_looks_like_a_placeholder_is_left_alone(tmp_path, provider_name):
    """A CV or a job description may contain `{{...}}`; that is data, not a placeholder of this prompt."""
    messages = [{"role": "system", "content": "Say {{what}}"}]
    provider = (
        LangfusePrompts(chat_client(messages)) if provider_name == "langfuse" else file_prompts(tmp_path, messages)
    )
    resolved = await provider.resolve(PromptRef(name="scorer"), {"what": "a template like {{other}}"})
    assert resolved.instructions == "Say a template like {{other}}"


async def test_publishing_a_chat_prompt_stores_its_messages_unrendered(tmp_path):
    provider = file_prompts(tmp_path)
    pinned = await pin_prompt(PromptRef(name="scorer", variables={"area": "Delivery"}), provider)

    assert pinned.snapshot is None
    assert [(m.role, m.content) for m in pinned.snapshot_messages] == [(m["role"], m["content"]) for m in CHAT]
    assert pinned.variables == {"area": "Delivery"}

    # The definition survives a round trip through Postgres and the file going away.
    (tmp_path / "scorer.chat.json").unlink()
    restored = PromptRef.model_validate_json(pinned.model_dump_json())
    resolved = await provider.resolve(restored, {"candidate_profile": "Ada"})
    assert resolved.instructions == "You score Delivery."
    assert resolved.messages[0].content == "Profile: Ada"


async def test_publishing_a_langfuse_chat_prompt_pins_the_version_instead():
    provider = LangfusePrompts(chat_client())
    pinned = await pin_prompt(PromptRef(name="scorer"), provider)
    assert (pinned.version, pinned.snapshot_messages) == (7, None)


def test_a_chat_snapshot_cannot_also_be_a_text_snapshot_or_a_version():
    message = {"role": "system", "content": "x"}
    with pytest.raises(ValidationError, match="not both"):
        PromptRef(name="scorer", snapshot="text", snapshot_messages=[message])
    with pytest.raises(ValidationError, match="snapshot"):
        PromptRef(name="scorer", snapshot_messages=[message], version=2)
    with pytest.raises(ValidationError, match="at least one message"):
        PromptRef(name="scorer", snapshot_messages=[])


def test_a_chat_snapshot_refuses_a_role_the_runtime_cannot_send():
    with pytest.raises(ValidationError):
        PromptRef(name="scorer", snapshot_messages=[{"role": "tool", "content": "x"}])


def test_render_is_a_single_pass_over_each_message():
    template = PromptTemplate(
        name="t",
        version=None,
        messages=(PromptMessage(role="system", content="{{a}} {{b}}"),),
        is_chat=False,
    )
    assert render(template, {"a": "{{b}}", "b": "second"}).instructions == "{{b}} second"


# ----- what the run is started from -------------------------------------------------


def test_a_text_prompt_sends_the_json_payload_exactly_as_before():
    sent = _run_input((), "Explain this.", {"application": 1})
    assert json.loads(sent) == {"request": "Explain this.", "context": {"application": 1}}


def test_a_chat_prompt_sends_its_own_messages_first():
    messages = (PromptMessage(role="user", content="Profile: Ada"),)
    sent = _run_input(messages, "", None)
    assert sent == [{"role": "user", "content": "Profile: Ada"}]


def test_a_chat_prompt_appends_the_payload_only_when_the_caller_sent_something():
    messages = (PromptMessage(role="user", content="Profile: Ada"),)
    sent = _run_input(messages, "Anything else?", {"application": 1})
    assert sent[0] == {"role": "user", "content": "Profile: Ada"}
    assert json.loads(sent[1]["content"]) == {"request": "Anything else?", "context": {"application": 1}}


class _Repository:
    def __init__(self, config):
        self._config = config

    async def get_active(self, *, agent_key, environment):
        return SimpleNamespace(config=self._config, version=3)


def _runtime(prompts, config):
    factory = AgentFactory(
        repository=_Repository(config),
        prompts=prompts,
        models=SimpleNamespace(
            resolve=lambda provider, name: name,
            capability=lambda provider, name: SimpleNamespace(tool_calling=True, structured_output=True),
            validated_settings=lambda settings: {},
        ),
        capabilities=SimpleNamespace(resolve_many=lambda keys, context: []),
        outputs=SimpleNamespace(resolve=lambda key: None),
        guardrails=SimpleNamespace(resolve_many=lambda keys: SimpleNamespace(input=[], output=[])),
    )
    return AgentRuntime(factory)


async def test_the_runtime_passes_request_variables_through_to_the_prompt(monkeypatch, tmp_path):
    from agents import Runner

    config = AgentConfig(
        name="Scorer",
        prompt=PromptRef(name="scorer", variables={"area": "Delivery"}),
        model={"provider": "openai", "name": "gpt-5-nano"},
    )
    runtime = _runtime(file_prompts(tmp_path), config)
    run = AsyncMock(return_value=SimpleNamespace(final_output="done"))
    monkeypatch.setattr(Runner, "run", run)

    result = await runtime.run(
        agent_key="scorer",
        environment="dev",
        prompt_variables={"candidate_profile": "Ada"},
        context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
    )

    assert result.output == "done"
    assert run.call_args.args[0].instructions == "You score Delivery."
    assert run.call_args.kwargs["input"] == [{"role": "user", "content": "Profile: Ada"}]


# ----- sub-agents ---------------------------------------------------------------------


async def test_a_sub_agents_prompt_is_filled_from_the_same_request_variables(tmp_path):
    """The parent received the values; its specialists must not fail for want of them."""
    (tmp_path / "specialist.chat.json").write_text(
        json.dumps([{"role": "system", "content": "You advise on {{area}}."}])
    )
    provider = file_prompts(tmp_path)

    configs = {
        "scorer": AgentConfig(
            name="Scorer",
            prompt=PromptRef(name="scorer"),
            model={"provider": "openai", "name": "gpt-5-nano"},
            sub_agents=[{"agent_key": "specialist", "environment": "dev", "mode": "tool"}],
        ),
        "specialist": AgentConfig(
            name="Specialist",
            prompt=PromptRef(name="specialist"),
            model={"provider": "openai", "name": "gpt-5-nano"},
        ),
    }

    class Repository:
        async def get_active(self, *, agent_key, environment):
            return SimpleNamespace(config=configs[agent_key], version=1)

    runtime = _runtime(provider, configs["scorer"])
    runtime.factory.repository = Repository()

    built = await runtime.factory.build(
        agent_key="scorer",
        environment="dev",
        context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        prompt_variables={"area": "Delivery", "candidate_profile": "Ada"},
    )
    assert built.agent.instructions == "You score Delivery."


async def test_a_sub_agent_whose_chat_prompt_has_messages_is_refused(tmp_path):
    """A sub-agent is handed its caller's input, so its own messages would be dropped in silence."""
    provider = file_prompts(tmp_path)  # `scorer` has a user message.
    config = AgentConfig(
        name="Parent",
        prompt=PromptRef(name="scorer"),
        model={"provider": "openai", "name": "gpt-5-nano"},
        sub_agents=[{"agent_key": "scorer", "environment": "dev", "mode": "tool"}],
    )
    runtime = _runtime(provider, config)
    with pytest.raises(PromptError, match="cannot send"):
        await runtime.factory.build(
            agent_key="parent",
            environment="dev",
            context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
            prompt_variables={"area": "Delivery", "candidate_profile": "Ada"},
        )


def test_a_text_prompt_run_with_nothing_to_say_is_refused():
    """Without opening messages and without input, the model would be asked to answer an empty request."""
    with pytest.raises(ValueError, match="needs an input or a context"):
        _run_input((), "", None)
