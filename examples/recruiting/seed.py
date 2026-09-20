"""Put the three recruiting agents in the registry and make them live.

    uv run python -m examples.recruiting.seed

Safe to run again: each run adds the next version and promotes it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from asas_agent import build_platform
from asas_agent.registry.schema import AgentConfig

from . import capabilities as _capabilities  # noqa: F401 - register before publication
from . import schemas as _schemas  # noqa: F401 - register before publication

AGENT_DIR = Path(__file__).parent / "agents"


async def seed(environment: str = "dev") -> None:
    platform = build_platform(require_prompts=False)
    try:
        for path in sorted(AGENT_DIR.glob("*.json")):
            agent_key = path.stem
            config = AgentConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))

            definition = await platform.repository.release(
                agent_key=agent_key,
                config=config,
                environment=environment,
                created_by="recruiting-demo",
            )
            print(f"{agent_key:<22} v{definition.version} published and live in {environment}")
    finally:
        await platform.close()


if __name__ == "__main__":
    asyncio.run(seed())
