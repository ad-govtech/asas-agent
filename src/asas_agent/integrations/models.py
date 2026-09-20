"""Model registry.

Configuration names a provider and a model, and only the providers here can be
reached. What a given model can do is the provider's business: asking a model
for structured output it cannot produce is refused by the provider, with its
own message, rather than by a table of model names this package would have to
keep in step with every release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SETTABLE_MODEL_SETTINGS = {"temperature", "top_p", "max_tokens", "reasoning", "reasoning_effort"}

#: Where a model can come from. Which models each one serves is its own business.
PROVIDERS = ("openai", "gateway")


class ModelError(RuntimeError):
    """Raised when a model or provider is not available."""


@dataclass
class ModelRegistry:
    """Resolves `provider:name` into something the Agents SDK accepts."""

    settings: Any

    def __post_init__(self) -> None:
        self._clients: dict[str, Any] = {}

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
            from agents import OpenAIResponsesModel
            from openai import AsyncOpenAI

            if "openai" not in self._clients:
                self._clients["openai"] = AsyncOpenAI(api_key=self.settings.openai_api_key)
            return OpenAIResponsesModel(model=name, openai_client=self._clients["openai"])

        if provider == "gateway":
            from agents import OpenAIChatCompletionsModel

            return OpenAIChatCompletionsModel(model=name, openai_client=self._gateway_client())

        raise ModelError(f"Unknown model provider {provider!r}. Known providers: {', '.join(PROVIDERS)}")

    def assert_known(self, provider: str) -> None:
        """Check the provider without building a client, so publication needs no credentials."""
        if provider not in PROVIDERS:
            raise ModelError(f"Unknown model provider {provider!r}. Known providers: {', '.join(PROVIDERS)}")

    def resolve_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Translate our reasoning alias, and reject settings the SDK does not take."""
        from agents import ModelSettings

        unknown = settings.keys() - SETTABLE_MODEL_SETTINGS
        if unknown:
            raise ModelError(f"Unknown model settings: {', '.join(sorted(unknown))}")
        translated = dict(settings)
        if "reasoning_effort" in translated:
            if "reasoning" in translated:
                raise ModelError("Set reasoning or reasoning_effort, not both")
            translated["reasoning"] = {"effort": translated.pop("reasoning_effort")}
        try:
            ModelSettings(**translated)
        except (TypeError, ValueError) as exc:
            raise ModelError(f"Invalid model settings: {exc}") from exc
        return translated

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
