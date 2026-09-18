"""Capability registry.

A capability is a name in configuration bound to trusted code in the app. The
database never supplies an implementation, an endpoint or a credential.

    @capability("policy.search", risk="read")
    def make_policy_search(context): ...
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

Risk = Literal["read", "action"]


class CapabilityError(RuntimeError):
    """Raised when a capability is unknown or not allowed for this caller."""


@dataclass(frozen=True)
class Capability:
    key: str
    risk: Risk
    factory: Callable[..., Any]
    description: str | None = None


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        if capability.key in self._capabilities:
            raise CapabilityError(f"{capability.key} is already registered")
        self._capabilities[capability.key] = capability

    def capability(self, key: str, *, risk: Risk = "read", description: str | None = None):
        """Decorator form: registers the decorated factory under `key`."""

        def decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
            self.register(Capability(key=key, risk=risk, factory=factory, description=description))
            return factory

        return decorator

    def get(self, key: str) -> Capability:
        capability = self._capabilities.get(key)
        if capability is None:
            known = ", ".join(sorted(self._capabilities)) or "none registered"
            raise CapabilityError(f"Unknown capability {key!r}. Registered: {known}")
        return capability

    def keys(self) -> list[str]:
        return sorted(self._capabilities)

    def resolve_many(self, keys: Iterable[str], context) -> list[Any]:
        """Build one request-bound tool per key, after the caller's policy allows it."""
        tools = []
        for key in keys:
            capability = self.get(key)
            context.assert_capability_allowed(capability)
            tools.append(capability.factory(context))
        return tools


#: The registry an application adds its tools to.
registry = CapabilityRegistry()
capability = registry.capability
