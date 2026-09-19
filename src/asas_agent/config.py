"""Runtime settings, read from the environment once per process."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "production"]


def normalize_dsn(dsn: str) -> str:
    """Accept the connection strings hosts hand out, return one asyncpg understands.

    Neon, Vercel and psql all print `postgres://…?sslmode=require`; asyncpg wants
    `postgresql+asyncpg://…` and rejects libpq-only query parameters.
    """
    if not dsn:
        return dsn

    url, _, query = dsn.partition("?")

    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql+asyncpg://"),
        ("postgresql://", "postgresql+asyncpg://"),
        ("postgres://", "postgresql+asyncpg://"),
    ):
        if url.startswith(prefix):
            url = replacement + url[len(prefix) :]
            break

    pairs = parse_qsl(query, keep_blank_values=True)
    ssl_values = [value for key, value in pairs if key in {"ssl", "sslmode"}]
    if len(set(ssl_values)) > 1:
        raise ValueError("Conflicting ssl and sslmode values in DATABASE_URL")
    keep = [(key, value) for key, value in pairs if key not in {"ssl", "sslmode", "channel_binding", "options"}]
    if ssl_values:
        mode = ssl_values[0]
        if mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError(f"Unsupported PostgreSQL SSL mode: {mode!r}")
        keep.append(("ssl", mode))
    return f"{url}?{urlencode(keep)}" if keep else url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Agent registry
    database_url: str = Field(default="", alias="DATABASE_URL")
    environment: Environment = Field(default="dev", alias="ASAS_ENVIRONMENT")
    registration_modules: str = Field(default="", alias="ASAS_REGISTRATION_MODULES")

    # Runtime API
    api_key: str = Field(default="", alias="ASAS_API_KEY")
    host: str = Field(default="0.0.0.0", alias="ASAS_HOST")
    port: int = Field(default=8080, alias="ASAS_PORT")

    # Files need no separate service; publishing snapshots their text in the registry.
    prompt_provider: Literal["langfuse", "file"] = Field(default="file", alias="ASAS_PROMPT_PROVIDER")
    prompt_dir: str = Field(default="prompts", alias="ASAS_PROMPT_DIR")
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")
    tracing_enabled: bool = Field(default=True, alias="ASAS_TRACING")
    tracing_provider: Literal["none", "langfuse"] = Field(default="none", alias="ASAS_TRACING_PROVIDER")

    # Models
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    gateway_base_url: str = Field(default="", alias="MODEL_GATEWAY_URL")
    gateway_api_key: str = Field(default="", alias="MODEL_GATEWAY_KEY")

    # Limits applied on top of whatever a definition asks for
    max_turns_ceiling: int = Field(default=20, ge=1, alias="ASAS_MAX_TURNS_CEILING")
    timeout_ceiling_seconds: int = Field(default=300, ge=1, alias="ASAS_TIMEOUT_CEILING")

    @field_validator("database_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return normalize_dsn(value)

    @property
    def langfuse_configured(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
