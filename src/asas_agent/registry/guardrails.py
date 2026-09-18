"""Guardrail registry.

Guardrails are named in configuration and implemented in the app, the same way
capabilities are. They check input before a run and output after it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


class GuardrailError(RuntimeError):
    """Raised when a guardrail name is not registered."""


@dataclass
class ResolvedGuardrails:
    input: list[Any] = field(default_factory=list)
    output: list[Any] = field(default_factory=list)


class GuardrailRegistry:
    def __init__(self) -> None:
        self._input: dict[str, Any] = {}
        self._output: dict[str, Any] = {}

    def register_input(self, key: str, guardrail: Any) -> None:
        self._input[key] = guardrail

    def register_output(self, key: str, guardrail: Any) -> None:
        self._output[key] = guardrail

    def resolve_many(self, keys: Iterable[str]) -> ResolvedGuardrails:
        resolved = ResolvedGuardrails()
        for key in keys:
            if key in self._input:
                resolved.input.append(self._input[key])
            elif key in self._output:
                resolved.output.append(self._output[key])
            else:
                known = ", ".join(sorted({*self._input, *self._output})) or "none registered"
                raise GuardrailError(f"Unknown guardrail {key!r}. Registered: {known}")
        return resolved

    def keys(self) -> list[str]:
        return sorted({*self._input, *self._output})


registry = GuardrailRegistry()
