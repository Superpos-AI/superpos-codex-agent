"""Codex-specific config: extends BaseConfig with OpenAI API key + Codex model knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass

from superpos_agent_core import BaseConfig


@dataclass
class CodexConfig(BaseConfig):
    """Adds Codex-specific knobs on top of the universal BaseConfig."""

    openai_api_key: str = ""
    codex_model: str = "gpt-5.5"
    codex_reasoning_effort: str = "high"

    def __post_init__(self) -> None:
        if not self.executor_kind or self.executor_kind == "generic":
            self.executor_kind = "codex"
        super().__post_init__()

    @classmethod
    def from_env(cls) -> "CodexConfig":
        base = cls._base_env_kwargs()

        # Honour the legacy CODEX_* env vars in addition to the generic ones,
        # so existing .env files keep working after the port.  CODEX_WORKING_DIR
        # and CODEX_WORKTREE_ISOLATION are read independently — pre-port the
        # old config always honoured the isolation flag even when working dir
        # was left at the /workspace default, and we must preserve that.
        working_dir = os.environ.get("CODEX_WORKING_DIR")
        if working_dir:
            base["executor_working_dir"] = working_dir
        isolation_env = os.environ.get("CODEX_WORKTREE_ISOLATION")
        if isolation_env is not None:
            base["executor_worktree_isolation"] = (
                isolation_env.lower() not in ("0", "false", "no")
            )
        elif working_dir:
            # Explicit CODEX_WORKING_DIR with no isolation flag — re-derive
            # auto-detection from the new working dir (the base loader saw the
            # old default).
            base["executor_worktree_isolation"] = os.path.isdir(
                os.path.join(working_dir, ".git")
            )
        if os.environ.get("CODEX_MAX_PARALLEL"):
            base["executor_max_parallel"] = int(os.environ["CODEX_MAX_PARALLEL"])
        if os.environ.get("CODEX_MAX_TURNS"):
            base["executor_max_turns"] = int(os.environ["CODEX_MAX_TURNS"])

        base.update(
            executor_kind="codex",
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            codex_model=os.environ.get("CODEX_MODEL", "gpt-5.5"),
            codex_reasoning_effort=os.environ.get("CODEX_REASONING_EFFORT", "high"),
        )
        return cls(**base)
