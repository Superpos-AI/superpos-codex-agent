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

    # The reasoning-effort ladder differs by model *family*, and not only at the
    # top: GPT-5.6 renamed the bottom "no reasoning" tier from "minimal" to
    # "none". A single global ladder can't express both, so each family gets its
    # own tuple and EFFORT_LEVELS is the union the core set_effort() validates
    # against; efforts_for_model() narrows it per model.

    # GPT-5.6 (Sol/Terra/Luna): bottom tier is "none" (no reasoning); the top
    # tiers "xhigh"/"max" are GPT-5.6-only.
    _GPT_5_6_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh", "max")

    # Older families (gpt-5.5 and below): bottom tier is "minimal" and they top
    # out at "high" — they never accepted "none", "xhigh", or "max".
    _LEGACY_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high")

    # Union of every effort any known model accepts. The core
    # RuntimeConfig.set_effort() rejects anything outside this tuple; the
    # per-model narrowing happens in efforts_for_model().
    EFFORT_LEVELS: tuple[str, ...] = (
        "none", "minimal", "low", "medium", "high", "xhigh", "max",
    )

    _EXTENDED_EFFORT_MODELS: frozenset[str] = frozenset(
        {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    )

    # The "no reasoning" bottom tier was renamed between families: legacy
    # "minimal" became GPT-5.6's "none". They are the same intent under two
    # names, so reconciliation must map one to the other rather than clamp a
    # no-reasoning deployment up to the most expensive tier.
    _BOTTOM_TIER_ALIASES: frozenset[str] = frozenset({"none", "minimal"})

    @classmethod
    def efforts_for_model(cls, model: str) -> tuple[str, ...]:
        """Reasoning-effort levels valid for ``model``.

        The GPT-5.6 trio accepts the ``none..max`` ladder.  Unknown/custom model
        ids get the full union — the known list is only a hint (``/model``
        accepts any valid id), so we can't second-guess a model we ship no
        profile for; the preflight probe is the backstop that rejects an
        actually-incompatible model+effort pair.  Every *known* older family
        uses ``minimal`` as its bottom tier and tops out at ``high``.
        """
        if model in cls._EXTENDED_EFFORT_MODELS:
            return cls._GPT_5_6_EFFORTS
        if model not in cls.KNOWN_MODELS:
            return cls.EFFORT_LEVELS
        return cls._LEGACY_EFFORTS

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
        """Reconcile a persisted effort the current model can't accept.

        Returns ``True`` when the effort was changed.  Called on every
        ``/model`` switch and once at :meth:`load` so a stale
        ``runtime_config.json`` (e.g. ``model=gpt-5.5, effort=max`` carried over
        from an earlier default) can't leave every ``codex exec`` rejected.

        The bottom "no reasoning" tier was renamed across families (legacy
        ``minimal`` ↔ GPT-5.6 ``none``), so an invalid bottom tier is *mapped*
        to the target family's bottom tier — never clamped up. Clamping to the
        highest valid tier would silently turn a no-reasoning deployment into
        the costliest one (``minimal`` → ``max`` on the gpt-5.6 default switch;
        ``none`` → ``high`` on a switch back to gpt-5.5). Only genuinely
        unsupported *upper* tiers (``xhigh``/``max`` on a legacy model) are
        clamped down to the highest tier the model accepts.
        """
        # A blank effort is the explicit "no override" state: ``from_env()``
        # forwards ``CODEX_REASONING_EFFORT=`` verbatim, and
        # ``_build_codex_command()``/``_build_preflight_command()`` then omit
        # ``-c model_reasoning_effort=…`` so the Codex CLI's own default stands.
        # It matches no family ladder, so without this guard it would fall to
        # the clamp branch and be promoted to the top tier (``max`` on
        # gpt-5.6, ``high`` on gpt-5.5) — the opposite of no reasoning. Leave
        # it untouched.
        if not self.effort:
            return False
        allowed = self.efforts_for_model(self.model)
        if self.effort in allowed:
            return False
        if self.effort in self._BOTTOM_TIER_ALIASES:
            reconciled = allowed[0]  # target family's bottom (no-reasoning) tier
        else:
            reconciled = allowed[-1]  # clamp an unsupported upper tier down
        log.warning(
            "Effort %r is invalid for model %r — reconciling to %r",
            self.effort, self.model, reconciled,
        )
        self.effort = reconciled
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
