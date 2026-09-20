"""Agent factory: configuration in, an Agents SDK agent out.

Nothing here is specific to one product. The factory resolves the prompt, the
model, the approved tools and the output schema, and records what it resolved
so the trace can be reproduced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from asas_agent.integrations.models import ModelRegistry
from asas_agent.integrations.prompts import PromptMessage, PromptProvider
from asas_agent.registry.capabilities import CapabilityRegistry
from asas_agent.registry.outputs import OutputSchemaRegistry
from asas_agent.registry.repository import AgentRepository
from asas_agent.registry.schema import AgentConfig
from asas_agent.runtime.context import RuntimeContext


@dataclass
class BuiltAgent:
    agent: Any
    agent_version: int
    prompt_version: int | None
    config: AgentConfig
    max_turns: int
    timeout_seconds: float
    #: The user and assistant messages of a chat prompt, which open the run.
    prompt_messages: tuple[PromptMessage, ...] = ()


class AgentFactory:
    def __init__(
        self,
        *,
        repository: AgentRepository,
        prompts: PromptProvider,
        models: ModelRegistry,
        capabilities: CapabilityRegistry,
        outputs: OutputSchemaRegistry,
    ):
        self.repository = repository
        self.prompts = prompts
        self.models = models
        self.capabilities = capabilities
        self.outputs = outputs

    async def build(
        self,
        *,
        agent_key: str,
        environment: str,
        context: RuntimeContext,
        inputs: dict[str, Any] | None = None,
    ) -> BuiltAgent:
        from agents import Agent, ModelSettings

        started = asyncio.get_running_loop().time()
        definition = await self.repository.get_active(agent_key=agent_key, environment=environment)
        config = definition.config

        async with asyncio.timeout_at(started + min(context.timeout_seconds, config.runtime.timeout_seconds)):
            resolved_prompt = await self.prompts.resolve(config.prompt, inputs)
            model = self.models.resolve(config.model.provider, config.model.name)
            tools = self.capabilities.resolve_many(config.tools, context)
            max_turns = min(context.max_turns, config.runtime.max_turns)
            timeout_seconds = min(context.timeout_seconds, config.runtime.timeout_seconds)

            context.trace_metadata.update(
                {
                    "agent_key": agent_key,
                    "agent_version": definition.version,
                    "environment": environment,
                    "prompt_name": resolved_prompt.name,
                    "prompt_version": resolved_prompt.version,
                    "model_provider": config.model.provider,
                    "model_name": config.model.name,
                    "toolset": list(config.tools),
                    # A request's values now help decide the instructions, so a
                    # trace records which names were filled and a digest of the
                    # text that resulted. The values themselves are the
                    # caller's data and are not copied here.
                    "inputs": list(resolved_prompt.filled),
                    "instructions_digest": sha256(resolved_prompt.instructions.encode("utf-8")).hexdigest()[:12],
                }
            )

            agent = Agent(
                name=config.name,
                instructions=resolved_prompt.instructions,
                model=model,
                model_settings=ModelSettings(**self.models.resolve_settings(config.model.settings)),
                tools=tools,
                output_type=self.outputs.resolve(config.output.schema_key),
            )

            return BuiltAgent(
                agent=agent,
                agent_version=definition.version,
                prompt_version=resolved_prompt.version,
                config=config,
                max_turns=max_turns,
                timeout_seconds=timeout_seconds,
                prompt_messages=resolved_prompt.messages,
            )
