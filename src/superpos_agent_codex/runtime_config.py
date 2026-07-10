"""Codex runtime config: registers known Codex models + reasoning effort levels."""

from __future__ import annotations

from superpos_agent_core import RuntimeConfig


class CodexRuntimeConfig(RuntimeConfig):
    """Runtime knobs specialized for Codex CLI."""

    KNOWN_MODELS: tuple[str, ...] = (
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.5-mini",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "o4-mini",
        "o3",
    )

    # GPT-5.6 (Sol/Terra/Luna) documents reasoning efforts none/low/medium/high/
    # xhigh/max. "max" must be selectable so /effort can reach the top tier; the
    # core RuntimeConfig.set_effort() rejects anything outside this tuple.
    EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")
