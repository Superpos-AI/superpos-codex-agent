"""Regression coverage for the default Codex model contract.

PR #19 switched the Codex fleet default from ``gpt-5.5`` to ``gpt-5.6-terra``.
Three surfaces encode that decision independently and must stay in lockstep:

  1. the ``CodexConfig`` dataclass default,
  2. the ``CODEX_MODEL`` environment fallback in ``from_env``,
  3. the ``CodexRuntimeConfig.KNOWN_MODELS`` registry that ``/model list`` exposes,

and the README configuration table documents the default for operators. These
tests pin all four so the value cannot drift in one place without failing here.
"""

from __future__ import annotations

import re
from pathlib import Path

from superpos_agent_codex.config import CodexConfig
from superpos_agent_codex.runtime_config import CodexRuntimeConfig

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "gpt-5.6-terra"
NEW_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


def test_dataclass_default_is_gpt_5_6_terra():
    assert CodexConfig().codex_model == DEFAULT_MODEL


def test_from_env_falls_back_to_default_when_unset(monkeypatch):
    """An unset CODEX_MODEL must resolve to the gpt-5.6-terra default."""
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert CodexConfig.from_env().codex_model == DEFAULT_MODEL


def test_from_env_honours_explicit_override(monkeypatch):
    """An explicit CODEX_MODEL still wins over the default (rollback path)."""
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")

    assert CodexConfig.from_env().codex_model == "gpt-5.5"


def test_known_models_expose_new_gpt_5_6_trio():
    for model in NEW_MODELS:
        assert model in CodexRuntimeConfig.KNOWN_MODELS


def test_default_model_is_a_known_model():
    """The shipped default must be selectable via /model, never orphaned."""
    assert DEFAULT_MODEL in CodexRuntimeConfig.KNOWN_MODELS


def test_readme_documents_the_actual_default():
    """The README config table must state the real default, not a stale one."""
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"`CODEX_MODEL`.*?Default:\s*([^\s|]+)", readme)
    assert match, "CODEX_MODEL row with a documented default not found in README"
    assert match.group(1) == DEFAULT_MODEL
