"""Codex runtime config: registers known Codex models + reasoning effort levels.

Reasoning-effort validity is **model-specific**, not global.  The GPT-5.6 trio
(Sol/Terra/Luna) documents the full ``none/low/medium/high/xhigh/max`` ladder;
earlier families (gpt-5.5 and below) top out at ``high``.  Treating the ladder
as global lets ``/effort max`` persist against a gpt-5.5 deployment, after which
every ``codex exec --model gpt-5.5 -c model_reasoning_effort=max`` is rejected by
the API while the runtime keeps advertising the agent as ready.  This subclass
gates effort by the selected model and reconciles the persisted effort whenever
``/model`` switches to a model that can't accept it.
"""

from __future__ import annotations

import logging

from superpos_agent_core import RuntimeConfig

log = logging.getLogger(__name__)


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

    # The top two tiers are GPT-5.6-only. Every other model (gpt-5.5 and below)
    # tops out at "high"; selecting xhigh/max there is rejected by the API.
    _EXTENDED_EFFORTS: tuple[str, ...] = ("xhigh", "max")
    _EXTENDED_EFFORT_MODELS: frozenset[str] = frozenset(
        {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    )

    @classmethod
    def efforts_for_model(cls, model: str) -> tuple[str, ...]:
        """Reasoning-effort levels valid for ``model``.

        The GPT-5.6 trio accepts the full ladder.  Unknown/custom model ids
        also get the full ladder — the known list is only a hint (``/model``
        accepts any valid id), so we can't second-guess a model we ship no
        profile for; the preflight probe is the backstop that rejects an
        actually-incompatible model+effort pair.  Every *known* older family
        tops out at ``high``.
        """
        if model in cls._EXTENDED_EFFORT_MODELS or model not in cls.KNOWN_MODELS:
            return cls.EFFORT_LEVELS
        return tuple(e for e in cls.EFFORT_LEVELS if e not in cls._EXTENDED_EFFORTS)

    def set_effort(self, effort: str) -> None:
        """Validate the requested effort against the *currently selected* model.

        Overrides the core level-only check so ``/effort max`` is rejected on a
        model that can't accept it, instead of persisting an invalid pair that
        breaks every subsequent task.
        """
        allowed = self.efforts_for_model(self.model)
        if effort not in allowed:
            raise ValueError(
                f"Effort {effort!r} is not valid for model {self.model!r} — "
                f"choose one of: {', '.join(allowed)}"
            )
        self.effort = effort
        self._save()

    def set_model(self, model: str) -> None:
        """Switch the model, then reconcile a now-invalid persisted effort."""
        if not self.MODEL_RE.match(model):
            raise ValueError(f"Not a valid model id: {model!r}")
        self.model = model
        self._reconcile_effort()
        self._save()

    def _reconcile_effort(self) -> bool:
        """Downgrade a persisted effort the current model can't accept.

        Returns ``True`` when the effort was changed.  Called on every
        ``/model`` switch and once at :meth:`load` so a stale
        ``runtime_config.json`` (e.g. ``model=gpt-5.5, effort=max`` carried over
        from an earlier default) can't leave every ``codex exec`` rejected.
        """
        allowed = self.efforts_for_model(self.model)
        if self.effort in allowed:
            return False
        downgraded = allowed[-1]  # highest tier the model still accepts
        log.warning(
            "Effort %r is invalid for model %r — downgrading to %r",
            self.effort, self.model, downgraded,
        )
        self.effort = downgraded
        return True

    @classmethod
    def load(cls, **kwargs) -> "CodexRuntimeConfig":
        """Load persisted knobs, then reconcile an invalid model+effort pair.

        A config file written by an older build (or hand-edited) may hold an
        effort the model no longer accepts; fix it once at startup so preflight
        and real tasks see a valid pair.
        """
        rc = super().load(**kwargs)
        if rc._reconcile_effort():
            rc._save()
        return rc
