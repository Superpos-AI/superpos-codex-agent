"""Regression coverage for the Codex reasoning-effort contract.

PR #19 exposed the GPT-5.6 (Sol/Terra/Luna) model family, whose documented
reasoning efforts include ``max``. ``CodexRuntimeConfig.EFFORT_LEVELS`` gates
which values ``/effort`` accepts — the core ``RuntimeConfig.set_effort()``
raises ``ValueError`` for anything outside it — so ``max`` must be registered
or the top reasoning tier is unreachable from the Telegram runtime controls.

These tests pin ``max`` at every surface: the level registry, the
``set_effort`` accept path, and the ``model_reasoning_effort=`` flag that
``_build_codex_command`` hands to the Codex CLI.
"""

from __future__ import annotations

import pytest

from superpos_agent_codex.runtime_config import CodexRuntimeConfig


def test_max_is_a_known_effort_level():
    assert "max" in CodexRuntimeConfig.EFFORT_LEVELS


def test_set_effort_accepts_max(mock_runtime):
    """/effort max must be selectable, not rejected as an invalid level."""
    mock_runtime.set_effort("max")

    assert mock_runtime.effort == "max"


def test_set_effort_still_rejects_unknown_levels(mock_runtime):
    """The validation is real: an unregistered level must still raise."""
    with pytest.raises(ValueError):
        mock_runtime.set_effort("ultra")


def test_max_effort_reaches_codex_command(executor, mock_runtime):
    """A selected max effort must flow through to the Codex CLI invocation."""
    mock_runtime.set_effort("max")

    cmd = executor._build_codex_command("hello")

    assert "-c" in cmd
    assert "model_reasoning_effort=max" in cmd
