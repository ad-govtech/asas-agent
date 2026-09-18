"""Runtime context: who is calling, and what they are allowed to reach.

Identity travels here, never in prompt text. Tools are built per request and
close over this context, so one caller's token is never reused for another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asas_agent.registry.capabilities import Capability, CapabilityError


@dataclass
class CapabilityPolicy:
    """What this caller may use, on top of what the agent definition lists."""

    allowed: set[str] | None = None  # None means "whatever the definition allows"
    allow_action_tools: bool = False

    def assert_allowed(self, capability: Capability) -> None:
        if self.allowed is not None and capability.key not in self.allowed:
            raise CapabilityError(f"{capability.key} is not allowed for this caller")
        if capability.risk == "action" and not self.allow_action_tools:
            raise CapabilityError(
                f"{capability.key} changes data and is not enabled for this caller. "
                "Enable action tools explicitly, and authorize the call in the owning service."
            )


@dataclass
class RuntimeContext:
    tenant_id: str
    user_id: str
    correlation_id: str

    #: Bearer token of the calling user, passed to business APIs so they authorize the real caller.
    access_token: str | None = None
    environment: str = "production"
    max_turns: int = 8
    timeout_seconds: int = 45

    policy: CapabilityPolicy = field(default_factory=CapabilityPolicy)
    dependencies: dict[str, Any] = field(default_factory=dict)
    trace_metadata: dict[str, Any] = field(default_factory=dict)

    def assert_capability_allowed(self, capability: Capability) -> None:
        self.policy.assert_allowed(capability)

    def dependency(self, name: str) -> Any:
        """A client the app registered at startup, for example a policy service client."""
        if name not in self.dependencies:
            raise KeyError(f"No dependency named {name!r} was provided to the runtime")
        return self.dependencies[name]
