"""Superpos-Agent-Codex entry point — wires CodexExecutor into the core orchestrator."""

from __future__ import annotations

import asyncio
import logging

from superpos_agent_core import run_agent

from .codex_executor import CodexExecutor
from .config import CodexConfig
from .runtime_config import CodexRuntimeConfig

log = logging.getLogger(__name__)


async def main() -> None:
    config = CodexConfig.from_env()
    runtime = CodexRuntimeConfig.load(
        default_model=config.codex_model,
        default_effort=config.codex_reasoning_effort,
        home_dir=config.home_dir,
    )

    def _factory(cfg, rt, superpos, gateway, persona):
        return CodexExecutor(cfg, rt, superpos, gateway, persona=persona)

    await run_agent(config, runtime, executor_factory=_factory)


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
