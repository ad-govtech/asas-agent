"""Model registry.

Configuration names a provider and a model. Only providers registered here can
be reached, and a definition that asks for a capability the model does not have
is refused at publish time rather than failing mid-conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SETTABLE_MODEL_SETTINGS = {"temperature", "top_p", "max_tokens", "reasoning_effort"}


class ModelError(RuntimeError):
    """Raised when a model or provider is not available."""


@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool = True
    structured_output: bool = True
    multimodal: bool = False


@dataclass
class ModelRegistry:
    """Resolves `provider:name` into something the Agents SDK accepts."""

    settings: Any
    capabilities: dict[str, ModelCapabilities] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._clients: dict[str, Any] = {}
        self.capabilities = {
            "openai:gpt-5.6-sol": ModelCapabilities(multimodal=True),
            "openai:gpt-5-nano": ModelCapabilities(),
            # Sovereign and in-country models are served through the gateway. Set
            # these from what the deployment actually supports.
            "gateway:jais": ModelCapabilities(tool_calling=False, structured_output=False),
            **self.capabilities,
        }

    def _gateway_client(self):
        if "gateway" not in self._clients:
            if not self.settings.gateway_base_url:
                raise ModelError("MODEL_GATEWAY_URL is not set, so gateway models cannot be used")
            from openai import AsyncOpenAI

            self._clients["gateway"] = AsyncOpenAI(
                api_key=self.settings.gateway_api_key or "unused",
                base_url=self.settings.gateway_base_url,
            )
        return self._clients["gateway"]

    def resolve(self, provider: str, name: str):
        if provider == "openai":
            if not self.settings.openai_api_key:
                raise ModelError("OPENAI_API_KEY is not set")
            return name

        if provider == "gateway":
            from agents import OpenAIChatCompletionsModel

            return OpenAIChatCompletionsModel(model=name, openai_client=self._gateway_client())

        raise ModelError(f"Unknown model provider {provider!r}. Known providers: openai, gateway")

    def capability(self, provider: str, name: str) -> ModelCapabilities:
        return self.capabilities.get(f"{provider}:{name}", ModelCapabilities())

    def validated_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Drop anything that is not a known model setting, so configuration cannot smuggle arguments."""
        return {k: v for k, v in settings.items() if k in SETTABLE_MODEL_SETTINGS}
