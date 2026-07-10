"""Regression coverage for the Codex reasoning-effort contract.

Reasoning-effort validity is **model-specific**. The GPT-5.6 (Sol/Terra/Luna)
trio documents the full ``none/low/medium/high/xhigh/max`` ladder; earlier
families (gpt-5.5 and below) use ``minimal`` as their bottom tier and top out at
``high``. Treating the ladder as global let ``/effort max`` persist against a
gpt-5.5 deployment, after which every
``codex exec --model gpt-5.5 -c model_reasoning_effort=max`` was rejected while
the runtime still advertised the agent as ready (PR #19 review). It also dropped
GPT-5.6's ``none`` tier, so ``CODEX_REASONING_EFFORT=none`` was reconciled up to
``max`` — turning an explicit no-reasoning deployment into the costliest one.

These tests pin: the level registry, model-specific ``set_effort`` validation,
effort reconciliation when ``/model`` switches to a lower-tier model, the same
reconciliation at ``load`` for a stale config file, and the
``model_reasoning_effort=`` flag that ``_build_codex_command`` hands the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from superpos_agent_codex.runtime_config import CodexRuntimeConfig


def _runtime(tmp_path, model: str, effort: str) -> CodexRuntimeConfig:
    return CodexRuntimeConfig(
        model=model,
        effort=effort,
        path=str(tmp_path / "runtime_config.json"),
    )


def test_max_is_a_known_effort_level():
    assert "max" in CodexRuntimeConfig.EFFORT_LEVELS


# --- model-specific effort validity --------------------------------------


def test_gpt_5_6_models_accept_the_full_ladder():
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        allowed = CodexRuntimeConfig.efforts_for_model(model)
        assert "xhigh" in allowed and "max" in allowed


def test_gpt_5_6_bottom_tier_is_none_not_minimal():
    """GPT-5.6 renamed the no-reasoning tier "minimal" -> "none" (PR #19 review)."""
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        allowed = CodexRuntimeConfig.efforts_for_model(model)
        assert "none" in allowed
        assert "minimal" not in allowed
        assert allowed[0] == "none"


def test_older_models_use_minimal_not_none():
    """Legacy families keep "minimal"; they never accepted GPT-5.6's "none"."""
    for model in ("gpt-5.5", "gpt-5.5-mini", "gpt-5.4", "o3"):
        allowed = CodexRuntimeConfig.efforts_for_model(model)
        assert "minimal" in allowed
        assert "none" not in allowed


def test_older_models_top_out_at_high():
    for model in ("gpt-5.5", "gpt-5.5-mini", "gpt-5.4", "o3"):
        allowed = CodexRuntimeConfig.efforts_for_model(model)
        assert "max" not in allowed and "xhigh" not in allowed
        assert allowed[-1] == "high"


def test_unknown_model_gets_full_ladder():
    """Custom/unknown ids can't be second-guessed — preflight is the backstop."""
    allowed = CodexRuntimeConfig.efforts_for_model("gpt-6.0-experimental")
    assert allowed == CodexRuntimeConfig.EFFORT_LEVELS


# --- set_effort is gated by the selected model ---------------------------


def test_set_effort_accepts_max_on_gpt_5_6(tmp_path):
    """/effort max must be selectable on a model that supports it."""
    rc = _runtime(tmp_path, model="gpt-5.6-terra", effort="high")
    rc.set_effort("max")
    assert rc.effort == "max"


def test_set_effort_rejects_max_on_gpt_5_5(tmp_path):
    """/effort max on gpt-5.5 must raise, not persist an invalid pair."""
    rc = _runtime(tmp_path, model="gpt-5.5", effort="high")
    with pytest.raises(ValueError):
        rc.set_effort("max")
    assert rc.effort == "high"


def test_set_effort_still_rejects_unknown_levels(tmp_path):
    """The validation is real: an unregistered level must still raise."""
    rc = _runtime(tmp_path, model="gpt-5.6-terra", effort="high")
    with pytest.raises(ValueError):
        rc.set_effort("ultra")


# --- /model reconciles a now-invalid persisted effort --------------------


def test_switching_to_lower_tier_model_downgrades_max_effort(tmp_path):
    """Switching gpt-5.6-terra(max) -> gpt-5.5 must downgrade effort to high."""
    rc = _runtime(tmp_path, model="gpt-5.6-terra", effort="max")
    rc.set_effort("max")
    rc.set_model("gpt-5.5")
    assert rc.model == "gpt-5.5"
    assert rc.effort == "high"


def test_switching_between_models_keeps_valid_effort(tmp_path):
    """A still-valid effort survives a /model switch untouched."""
    rc = _runtime(tmp_path, model="gpt-5.6-terra", effort="medium")
    rc.set_model("gpt-5.5")
    assert rc.effort == "medium"


def test_reconciled_effort_is_persisted(tmp_path):
    """The downgrade must be written to disk, not just held in memory."""
    rc = _runtime(tmp_path, model="gpt-5.6-terra", effort="max")
    rc.set_effort("max")
    rc.set_model("gpt-5.5")
    saved = json.loads(Path(rc._path).read_text())
    assert saved == {"model": "gpt-5.5", "effort": "high"}


# --- load reconciles a stale config file ---------------------------------


def test_load_downgrades_stale_invalid_effort(tmp_path):
    """A config file with model=gpt-5.5, effort=max must load as effort=high."""
    path = tmp_path / "runtime_config.json"
    path.write_text(json.dumps({"model": "gpt-5.5", "effort": "max"}))
    rc = CodexRuntimeConfig.load(
        default_model="gpt-5.6-terra",
        default_effort="high",
        home_dir=str(tmp_path),
    )
    assert rc.model == "gpt-5.5"
    assert rc.effort == "high"
    # and the fix is persisted back so it happens once
    saved = json.loads(path.read_text())
    assert saved["effort"] == "high"


def test_load_keeps_valid_effort(tmp_path):
    """A valid persisted pair loads unchanged."""
    path = tmp_path / "runtime_config.json"
    path.write_text(json.dumps({"model": "gpt-5.6-terra", "effort": "max"}))
    rc = CodexRuntimeConfig.load(
        default_model="gpt-5.6-terra",
        default_effort="high",
        home_dir=str(tmp_path),
    )
    assert rc.effort == "max"


def test_load_preserves_none_effort_on_gpt_5_6(tmp_path):
    """CODEX_REASONING_EFFORT=none on gpt-5.6 must stay "none", not become "max".

    Regression for PR #19 review: the effort registry dropped GPT-5.6's "none"
    tier, so a fresh load with default_effort="none" (no config file yet) failed
    validation and _reconcile_effort() bumped it up to the highest valid tier —
    silently turning an explicit no-reasoning deployment into the costliest one.
    """
    rc = CodexRuntimeConfig.load(
        default_model="gpt-5.6-terra",
        default_effort="none",
        home_dir=str(tmp_path),
    )
    assert rc.model == "gpt-5.6-terra"
    assert rc.effort == "none"


def test_set_effort_accepts_none_on_gpt_5_6(tmp_path):
    """/effort none must be selectable on GPT-5.6 (no-reasoning tier)."""
    rc = _runtime(tmp_path, model="gpt-5.6-terra", effort="high")
    rc.set_effort("none")
    assert rc.effort == "none"


# --- effort flows through to the Codex CLI invocation --------------------


def test_max_effort_reaches_codex_command(executor, mock_runtime):
    """A selected max effort must flow through to the Codex CLI invocation."""
    mock_runtime.model = "gpt-5.6-terra"
    mock_runtime.set_effort("max")

    cmd = executor._build_codex_command("hello")

    assert "-c" in cmd
    assert "model_reasoning_effort=max" in cmd
