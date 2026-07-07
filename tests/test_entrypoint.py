import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO_ROOT / "entrypoint.sh"


def _read_entrypoint() -> str:
    return ENTRYPOINT.read_text()


def _module_setup_block() -> str:
    """Return the `module_setup` invocation (from the command through the
    trailing `|| echo` fallback), so arg assertions don't accidentally match
    an unrelated part of the script."""
    text = _read_entrypoint()
    m = re.search(
        r"python3 -m superpos_agent_core\.module_setup\b.*?\|\| echo[^\n]*",
        text,
        re.DOTALL,
    )
    assert m, "could not locate the module_setup invocation in entrypoint.sh"
    return m.group(0)


def test_module_setup_invoked():
    block = _module_setup_block()
    assert "--modules-dir" in block
    assert "--agents-md" in block
    assert "--bin-dir" in block


def test_skills_dir_passed():
    """module_setup must receive --skills-dir so the registry SKILLS overlay
    runs. Without it, module_setup overlays registry *modules* only and
    silently skips the skills half (registry.skills_overlay_skipped
    reason=no_skills_dir), leaving Codex agents on baked-in skills alone —
    the bug this fixes (analog of superpos-claude-agent #39)."""
    block = _module_setup_block()
    assert "--skills-dir" in block, (
        "module_setup must be invoked with --skills-dir so registry skills "
        "are overlaid into the Codex skills dir"
    )


def test_skills_dir_points_at_codex_skills():
    """The overlay target must be the Codex skills dir documented in AGENTS.md
    (`.codex/skills`) so materialised registry skills are actually discovered
    by the Codex CLI."""
    block = _module_setup_block()
    m = re.search(r"--skills-dir\s+(\S+)", block)
    assert m, "module_setup must pass a value for --skills-dir"
    assert m.group(1) == "/workspace/.codex/skills", (
        "module_setup --skills-dir must point at /workspace/.codex/skills "
        "(matching --modules-dir /workspace/.codex/modules and the skills "
        "path AGENTS.md documents)"
    )


def test_skills_layout_is_codex():
    """module_setup must receive --skills-layout codex.

    The @openai/codex CLI's skill loader only registers a skill when it finds
    a *directory* under a scanned root (e.g. /workspace/.codex/skills)
    containing a file named exactly SKILL.md. A flat <slug>.md file is
    invisible to Codex — which is why registry skills (and the baked
    plan/review/summarize) never appeared in the native skill list. The
    'codex' layout makes the overlay write dir-per-skill <slug>/SKILL.md so
    Codex discovers them."""
    block = _module_setup_block()
    m = re.search(r"--skills-layout\s+(\S+)", block)
    assert m, "module_setup must be invoked with --skills-layout for Codex"
    assert m.group(1) == "codex", (
        "module_setup --skills-layout must be 'codex' so the overlay writes "
        "dir-per-skill <slug>/SKILL.md that the Codex CLI can discover"
    )


def test_module_setup_fallback_is_non_fatal():
    """A registry/module-setup failure must not abort container startup — the
    baked-in skills remain the fallback."""
    block = _module_setup_block()
    assert "|| echo" in block, (
        "module_setup must stay non-fatal (|| echo Warning...) so a "
        "registry-fetch failure degrades to baked-in skills"
    )


# ── Baked-in skills must use the Codex dir-per-skill layout ──────────

SKILLS_DIR = REPO_ROOT / "workspace" / ".codex" / "skills"


def test_baked_skills_use_dir_per_skill_layout():
    """Baked-in skills must be <slug>/SKILL.md dirs, not flat <slug>.md files.

    Codex only discovers a skill dir containing SKILL.md; a flat <slug>.md
    at the skills root is never registered. So the baked plan/review/summarize
    skills must ship as directories — otherwise they never show in the Codex
    skill list (the original bug), independent of the registry overlay."""
    assert SKILLS_DIR.is_dir(), f"missing baked skills dir {SKILLS_DIR}"
    # No stray flat <slug>.md at the skills root.
    flat = [p.name for p in SKILLS_DIR.glob("*.md")]
    assert flat == [], (
        f"baked skills must be dir-per-skill (<slug>/SKILL.md); found flat "
        f"markdown files at the skills root that Codex will ignore: {flat}"
    )
    # Every baked skill dir carries a SKILL.md.
    skill_dirs = [d for d in SKILLS_DIR.iterdir() if d.is_dir()]
    assert skill_dirs, "expected at least one baked <slug>/ skill dir"
    for d in skill_dirs:
        assert (d / "SKILL.md").is_file(), (
            f"baked skill {d.name!r} is missing SKILL.md — Codex will not "
            f"register it"
        )


def test_baked_skills_present():
    """The three platform skills ship baked-in as the offline fallback."""
    for slug in ("plan", "review", "summarize"):
        assert (SKILLS_DIR / slug / "SKILL.md").is_file(), (
            f"expected baked skill {slug}/SKILL.md"
        )
