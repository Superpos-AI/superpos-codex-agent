"""Codex runtime config: registers known Codex models + reasoning effort levels."""

from __future__ import annotations

from superpos_agent_core import RuntimeConfig


class CodexRuntimeConfig(RuntimeConfig):
    """Runtime knobs specialized for Codex CLI."""

    KNOWN_MODELS: tuple[str, ...] = (
        "gpt-5.5",
        "gpt-5.5-mini",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "o4-mini",
        "o3",
    )

    EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")
