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


def test_module_setup_fallback_is_non_fatal():
    """A registry/module-setup failure must not abort container startup — the
    baked-in skills remain the fallback."""
    block = _module_setup_block()
    assert "|| echo" in block, (
        "module_setup must stay non-fatal (|| echo Warning...) so a "
        "registry-fetch failure degrades to baked-in skills"
    )
