"""Output schema registry.

Configuration names an output schema; the app registers the Pydantic model that
name means. An agent that asks for an unknown schema fails validation.
"""

from __future__ import annotations

from pydantic import BaseModel


class OutputSchemaError(RuntimeError):
    """Raised when an output schema name is not registered."""


class OutputSchemaRegistry:
    def __init__(self) -> None:
        self._schemas: dict[str, type[BaseModel]] = {}

    def register(self, key: str, model: type[BaseModel]) -> type[BaseModel]:
        if key in self._schemas:
            raise OutputSchemaError(f"{key} is already registered")
        self._schemas[key] = model
        return model

    def schema(self, key: str):
        """Decorator form: `@output_schema("customer_advice_v1")` above a model."""

        def decorator(model: type[BaseModel]) -> type[BaseModel]:
            return self.register(key, model)

        return decorator

    def resolve(self, key: str | None) -> type[BaseModel] | None:
        if key is None:
            return None
        model = self._schemas.get(key)
        if model is None:
            known = ", ".join(sorted(self._schemas)) or "none registered"
            raise OutputSchemaError(f"Unknown output schema {key!r}. Registered: {known}")
        return model

    def keys(self) -> list[str]:
        return sorted(self._schemas)


registry = OutputSchemaRegistry()
output_schema = registry.schema
