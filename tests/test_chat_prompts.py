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
    PromptShapeError,
    PromptTemplate,
    PromptVariableError,
    pin_prompt,
    render,
)
from asas_agent.registry.schema import AgentConfig, PromptRef
from asas_agent.runtime.context import RuntimeContext
from asas_agent.runtime.factory import AgentFactory
from asas_agent.runtime.runner import AgentRuntime, RunInputError, _run_input

CHAT = [
    {"role": "system", "content": "You score {{area}}."},
    {"role": "user", "content": "Profile: {{candidate_profile}}"},
]


def chat_client(messages=None, version=7):
    """A Langfuse client holding a real `ChatPromptClient`, so the tests meet the real parser."""
    langfuse_api = pytest.importorskip("langfuse.api.resources.prompts")
    from langfuse.model import ChatPromptClient

    raw = messages if messages is not None else CHAT
    prompt = ChatPromptClient(
        langfuse_api.Prompt_Chat(
            name="scorer", version=version, prompt=raw, config={}, labels=["production"], tags=[], type="chat"
        )
    )
    client = MagicMock()
    client.get_prompt.return_value = prompt
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


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "system", "content": "First."}, {"role": "system", "content": "Second."}],
        [{"role": "system", "content": "Go."}, {"role": "user", "content": "A"}, {"role": "user", "content": "B"}],
        [{"role": "user", "content": "A"}, {"role": "system", "content": "Go."}],
    ],
)
async def test_a_shape_other_than_one_system_and_one_user_is_refused(messages):
    """The narrow shape is the whole contract: it is what a reader can hold in their head."""
    provider = LangfusePrompts(chat_client(messages))
    with pytest.raises(PromptShapeError, match="one system message"):
        await provider.resolve(PromptRef(name="scorer"))


async def test_a_text_prompt_still_becomes_instructions_with_no_messages(tmp_path):
    (tmp_path / "advisor.md").write_text("Only instructions", encoding="utf-8")
    resolved = await FilePrompts(tmp_path).resolve(PromptRef(name="advisor"))
    assert resolved.instructions == "Only instructions"
    assert resolved.messages == ()


async def test_a_chat_prompt_without_a_system_message_is_refused():
    provider = LangfusePrompts(chat_client([{"role": "user", "content": "Just do it"}]))
    with pytest.raises(PromptShapeError, match="one system message"):
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


def test_a_prompt_that_asks_its_own_question_needs_nothing_from_the_caller():
    messages = (PromptMessage(role="user", content="Profile: Ada"),)
    assert _run_input(messages, "") == [{"role": "user", "content": "Profile: Ada"}]


def test_a_caller_message_follows_the_prompts_own():
    messages = (PromptMessage(role="user", content="Profile: Ada"),)
    assert _run_input(messages, "Anything else?") == [
        {"role": "user", "content": "Profile: Ada"},
        {"role": "user", "content": "Anything else?"},
    ]


def test_a_prompt_with_only_instructions_is_started_by_the_callers_message():
    assert _run_input((), "Explain this.") == [{"role": "user", "content": "Explain this."}]


def _configured_repository(configs):
    class Repository:
        async def get_active(self, *, agent_key, environment):
            return SimpleNamespace(config=configs[agent_key], version=1)

    return Repository()


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
        inputs={"candidate_profile": "Ada"},
        context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
    )

    assert result.output == "done"
    assert run.call_args.args[0].instructions == "You score Delivery."
    assert run.call_args.kwargs["input"] == [{"role": "user", "content": "Profile: Ada"}]


# ----- sub-agents ---------------------------------------------------------------------


async def test_a_sub_agent_whose_chat_prompt_has_messages_is_refused(tmp_path):
    """A sub-agent is handed its caller's input, so its own messages would be dropped in silence."""
    (tmp_path / "parent.chat.json").write_text(json.dumps([{"role": "system", "content": "You lead."}]))
    (tmp_path / "specialist.chat.json").write_text(
        json.dumps([{"role": "system", "content": "You advise."}, {"role": "user", "content": "Go ahead."}])
    )
    provider = file_prompts(tmp_path)

    configs = {
        "parent": AgentConfig(
            name="Parent",
            prompt=PromptRef(name="parent"),
            model={"provider": "openai", "name": "gpt-5-nano"},
            sub_agents=[{"agent_key": "specialist", "environment": "dev", "mode": "tool"}],
        ),
        "specialist": AgentConfig(
            name="Specialist",
            prompt=PromptRef(name="specialist"),
            model={"provider": "openai", "name": "gpt-5-nano"},
        ),
    }
    runtime = _runtime(provider, configs["parent"])
    runtime.factory.repository = _configured_repository(configs)

    with pytest.raises(PromptError, match="cannot send"):
        await runtime.factory.build(
            agent_key="parent",
            environment="dev",
            context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        )


def test_a_run_with_nothing_to_say_is_refused():
    """With neither a question in the prompt nor one from the caller, there is nothing to answer."""
    with pytest.raises(RunInputError, match="needs a message to answer"):
        _run_input((), "")


# ----- placeholder syntax, matching Langfuse exactly -----------------------------------


@pytest.mark.parametrize("name", ["candidate.name", "first-name", "user name"])
@pytest.mark.parametrize("provider_name", ["langfuse", "file"])
async def test_a_placeholder_name_is_whatever_sits_between_the_braces(tmp_path, provider_name, name):
    """Langfuse takes everything up to the next `}}`, so dots, dashes and spaces are all names."""
    messages = [{"role": "system", "content": "Score {{" + name + "}}."}]
    provider = (
        LangfusePrompts(chat_client(messages)) if provider_name == "langfuse" else file_prompts(tmp_path, messages)
    )

    with pytest.raises(PromptVariableError, match="needs values"):
        await provider.resolve(PromptRef(name="scorer"))

    resolved = await provider.resolve(PromptRef(name="scorer"), {name: "Ada"})
    assert resolved.instructions == "Score Ada."


@pytest.mark.parametrize("provider_name", ["langfuse", "file"])
async def test_both_providers_render_a_value_the_same_way(tmp_path, provider_name):
    """Moving an agent between file and Langfuse prompts must not change what the model reads."""
    messages = [{"role": "system", "content": "Profile: {{profile}}. Note: {{note}}."}]
    provider = (
        LangfusePrompts(chat_client(messages)) if provider_name == "langfuse" else file_prompts(tmp_path, messages)
    )
    resolved = await provider.resolve(PromptRef(name="scorer"), {"profile": {"yrs": 7}, "note": None})
    # `str()` for values and nothing for None, which is what Langfuse does.
    assert resolved.instructions == "Profile: {'yrs': 7}. Note: ."


async def test_a_variable_named_self_is_just_a_name():
    """Rendering is ours, so no value collides with a method argument."""
    provider = LangfusePrompts(chat_client([{"role": "system", "content": "Be {{self}}."}]))
    resolved = await provider.resolve(PromptRef(name="scorer"), {"self": "brief"})
    assert resolved.instructions == "Be brief."


async def test_every_missing_variable_is_reported_at_once(tmp_path):
    """One round trip per fix, not one per message."""
    provider = file_prompts(
        tmp_path,
        [{"role": "system", "content": "{{one}}"}, {"role": "user", "content": "{{two}}"}],
    )
    with pytest.raises(PromptVariableError, match="one, two"):
        await provider.resolve(PromptRef(name="scorer"))


# ----- prompt shape -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([{"role": "system", "content": "   "}, {"role": "user", "content": "go"}], "it is empty"),
        ([{"role": "user", "content": "go"}], "one system message"),
        ([{"role": "user", "content": "Q"}, {"role": "system", "content": "Now answer"}], "one system message"),
    ],
)
async def test_a_prompt_that_cannot_instruct_an_agent_is_refused(tmp_path, messages, expected):
    provider = file_prompts(tmp_path, messages)
    with pytest.raises(PromptShapeError, match=expected):
        await provider.resolve(PromptRef(name="scorer"))


async def test_a_broken_prompt_is_refused_at_publication_not_on_every_run(tmp_path):
    """Publishing is where configuration is checked; a run is too late to find this."""
    provider = file_prompts(tmp_path, [{"role": "user", "content": "Just do it"}])
    with pytest.raises(PromptShapeError, match="one system message"):
        await pin_prompt(PromptRef(name="scorer"), provider)


async def test_message_content_must_be_text(tmp_path):
    """A content part list would otherwise reach the model as a Python repr."""
    provider = file_prompts(tmp_path, [{"role": "system", "content": [{"type": "text", "text": "hi"}]}])
    with pytest.raises(PromptError, match="not text"):
        await provider.resolve(PromptRef(name="scorer"))


# ----- prompt files -------------------------------------------------------------------


@pytest.mark.parametrize("content", ['{"role": "system"}', "[1, 2]", "not json at all"])
async def test_a_malformed_chat_prompt_file_is_refused(tmp_path, content):
    (tmp_path / "scorer.chat.json").write_text(content)
    with pytest.raises(PromptError):
        await FilePrompts(tmp_path).resolve(PromptRef(name="scorer"))


@pytest.mark.parametrize("name", ["../outside", "/tmp/outside"])
async def test_a_chat_prompt_file_cannot_escape_the_prompt_directory(tmp_path, name):
    (tmp_path / "scorer.chat.json").write_text(json.dumps(CHAT))
    with pytest.raises(PromptError, match="inside ASAS_PROMPT_DIR"):
        await FilePrompts(tmp_path).resolve(PromptRef(name=name))


async def test_a_chat_prompt_file_wins_over_a_text_file_of_the_same_name(tmp_path):
    (tmp_path / "scorer.md").write_text("Text version", encoding="utf-8")
    provider = file_prompts(tmp_path, [{"role": "system", "content": "Chat version"}])
    assert (await provider.resolve(PromptRef(name="scorer"))).instructions == "Chat version"


# ----- what a request may fill ---------------------------------------------------------


async def test_inputs_larger_than_the_ceiling_are_refused(tmp_path):
    runtime = _runtime(file_prompts(tmp_path), None)
    runtime.inputs_max_bytes = 100
    with pytest.raises(RunInputError, match="over the 100 byte limit"):
        await runtime.run(
            agent_key="scorer",
            environment="dev",
            inputs={"candidate_profile": "x" * 200},
            context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        )


# ----- the interfaces a caller reaches -------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (PromptVariableError("needs values for: area"), 400),
        (RunInputError("nothing to start from"), 400),
        (PromptShapeError("no system message"), 500),
    ],
)
async def test_the_api_answers_a_caller_error_and_a_broken_definition_differently(monkeypatch, error, status):
    import httpx

    from asas_agent.api.app import create_app
    from asas_agent.config import Settings

    settings = Settings(_env_file=None, DATABASE_URL="postgresql://test@localhost/test", OPENAI_API_KEY="test")
    platform = SimpleNamespace(
        settings=settings,
        runtime=SimpleNamespace(run=AsyncMock(side_effect=error), default_dependencies={}),
    )
    body = {
        "agent_key": "scorer",
        "input": "hi",
        "inputs": {"candidate_profile": "Ada"},
        "execution": {"tenant_id": "T1", "user_id": "U1", "correlation_id": "R1"},
    }
    transport = httpx.ASGITransport(app=create_app(platform))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/agents/run", json=body)

    assert response.status_code == status
    assert platform.runtime.run.call_args.kwargs["inputs"] == {"candidate_profile": "Ada"}


def test_the_cli_passes_variables_and_rejects_a_pair_without_an_equals(monkeypatch):
    from typer.testing import CliRunner

    from asas_agent.cli.main import app as cli_app

    run = AsyncMock(
        return_value=SimpleNamespace(output="done", agent_version=1, prompt_version=None, trace_id=None, toolset=[])
    )
    platform = SimpleNamespace(runtime=SimpleNamespace(run=run), close=AsyncMock())
    monkeypatch.setattr("asas_agent.cli.main._platform", lambda *a, **k: platform)

    result = CliRunner().invoke(cli_app, ["run", "scorer", "--input", "area=Delivery", "--input", "note=a=b"])
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["inputs"] == {"area": "Delivery", "note": "a=b"}

    assert CliRunner().invoke(cli_app, ["run", "scorer", "--input", "area"]).exit_code != 0


async def test_a_trace_records_which_variables_were_filled_but_not_their_values(tmp_path):
    """The values are the caller's data; the digest still identifies the instructions that ran."""
    config = AgentConfig(
        name="Scorer",
        prompt=PromptRef(name="scorer", variables={"area": "Delivery"}),
        model={"provider": "openai", "name": "gpt-5-nano"},
    )
    runtime = _runtime(file_prompts(tmp_path), config)
    context = RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1")

    await runtime.factory.build(
        agent_key="scorer",
        environment="dev",
        context=context,
        inputs={"candidate_profile": "Ada Lovelace"},
    )

    assert context.trace_metadata["inputs"] == ["area", "candidate_profile"]
    assert len(context.trace_metadata["instructions_digest"]) == 12
    assert "Ada Lovelace" not in json.dumps(context.trace_metadata)


async def test_a_sub_agent_is_filled_by_its_own_definition_only(tmp_path):
    """Nothing is forwarded: the caller addressed the parent and cannot know what a specialist needs."""
    (tmp_path / "parent.chat.json").write_text(json.dumps([{"role": "system", "content": "You lead {{area}}."}]))
    (tmp_path / "specialist.chat.json").write_text(
        json.dumps([{"role": "system", "content": "You advise on {{topic}}."}])
    )
    configs = {
        "parent": AgentConfig(
            name="Parent",
            prompt=PromptRef(name="parent"),
            model={"provider": "openai", "name": "gpt-5-nano"},
            sub_agents=[{"agent_key": "specialist", "environment": "dev", "mode": "tool"}],
        ),
        "specialist": AgentConfig(
            name="Specialist",
            prompt=PromptRef(name="specialist", variables={"topic": "delivery risk"}),
            model={"provider": "openai", "name": "gpt-5-nano"},
        ),
    }
    runtime = _runtime(FilePrompts(tmp_path), configs["parent"])
    runtime.factory.repository = _configured_repository(configs)

    built = await runtime.factory.build(
        agent_key="parent",
        environment="dev",
        context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        inputs={"area": "Delivery"},
    )
    assert built.agent.instructions == "You lead Delivery."


async def test_a_sub_agent_whose_definition_leaves_a_value_unset_is_refused(tmp_path):
    """Better a clear failure at build than a `{{topic}}` in the specialist's instructions."""
    (tmp_path / "parent.chat.json").write_text(json.dumps([{"role": "system", "content": "You lead."}]))
    (tmp_path / "specialist.chat.json").write_text(
        json.dumps([{"role": "system", "content": "You advise on {{topic}}."}])
    )
    configs = {
        "parent": AgentConfig(
            name="Parent",
            prompt=PromptRef(name="parent"),
            model={"provider": "openai", "name": "gpt-5-nano"},
            sub_agents=[{"agent_key": "specialist", "environment": "dev", "mode": "tool"}],
        ),
        "specialist": AgentConfig(
            name="Specialist",
            prompt=PromptRef(name="specialist"),
            model={"provider": "openai", "name": "gpt-5-nano"},
        ),
    }
    runtime = _runtime(FilePrompts(tmp_path), configs["parent"])
    runtime.factory.repository = _configured_repository(configs)

    with pytest.raises(PromptVariableError, match="topic"):
        await runtime.factory.build(
            agent_key="parent",
            environment="dev",
            context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
            inputs={"topic": "ignored"},
        )


@pytest.mark.parametrize("provider_name", ["langfuse", "file"])
async def test_a_value_the_prompt_does_not_ask_for_is_refused(tmp_path, provider_name):
    """It would not reach the model, so sending it is a mistake, not a no-op."""
    provider = LangfusePrompts(chat_client()) if provider_name == "langfuse" else file_prompts(tmp_path)
    ref = PromptRef(name="scorer", variables={"area": "Delivery"})

    with pytest.raises(PromptVariableError, match="does not use tone"):
        await provider.resolve(ref, {"candidate_profile": "Ada", "tone": "casual"})


async def test_what_a_request_may_fill_is_what_is_left_unanswered(tmp_path):
    provider = file_prompts(tmp_path)
    ref = PromptRef(name="scorer", variables={"area": "Delivery"})

    with pytest.raises(PromptVariableError, match="asks for: candidate_profile"):
        await provider.resolve(ref, {"unknown": "x"})

    assert (await provider.resolve(ref, {"candidate_profile": "Ada"})).messages[0].content == "Profile: Ada"


# ----- naming a run --------------------------------------------------------------------


async def test_a_run_is_named_after_its_agent_unless_the_caller_says_otherwise(tmp_path, monkeypatch):
    from agents import Runner

    config = AgentConfig(
        name="Scorer",
        prompt=PromptRef(name="scorer", variables={"area": "Delivery", "candidate_profile": "Ada"}),
        model={"provider": "openai", "name": "gpt-5-nano"},
    )
    runtime = _runtime(file_prompts(tmp_path), config)
    monkeypatch.setattr(Runner, "run", AsyncMock(return_value=SimpleNamespace(final_output="done")))
    context = RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1")

    default = await runtime.run(agent_key="scorer", environment="dev", context=context)
    assert default.run_name == "agent:scorer"

    named = await runtime.run(agent_key="scorer", environment="dev", run_name="scorer-Delivery", context=context)
    assert named.run_name == "scorer-Delivery"


@pytest.mark.parametrize(
    "name",
    ["", "   ", "agent:payroll-approver", "x" * 201, "scorer\x1b[2Jwiped", "scor​er"],
)
async def test_a_run_name_that_cannot_be_trusted_is_refused(tmp_path, name):
    """The name is what a person and an evaluation harness read to know which run this was."""
    config = AgentConfig(
        name="Scorer",
        prompt=PromptRef(name="scorer", variables={"area": "D", "candidate_profile": "A"}),
        model={"provider": "openai", "name": "gpt-5-nano"},
    )
    runtime = _runtime(file_prompts(tmp_path), config)
    with pytest.raises(RunInputError):
        await runtime.run(
            agent_key="scorer",
            environment="dev",
            run_name=name,
            context=RuntimeContext(tenant_id="T1", user_id="U1", correlation_id="R1"),
        )
