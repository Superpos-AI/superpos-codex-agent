import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Minimum agent-core version the entrypoint depends on. Bump this in lockstep
# with any new core API the entrypoint starts depending on.
#
# 0.1.2  — ships the `github_auth` module the entrypoint invokes
#          (`python3 -m superpos_agent_core.github_auth setup`); an older core
#          silently leaves git/gh auth unconfigured.
# 0.1.12 — module_setup honours `--skills-dir` (the registry SKILLS overlay).
#          The entrypoint passes `--skills-dir /workspace/.agents/skills`,
#          so the floor must guarantee that support or registry skills never
#          reach Codex agents.
MIN_CORE = (0, 1, 12)


def _lower_bound(spec: str) -> tuple[int, int, int]:
    m = re.search(r"superpos-agent-core>=(\d+)\.(\d+)\.(\d+)", spec)
    assert m, f"no pinned superpos-agent-core lower bound found in: {spec!r}"
    return tuple(int(g) for g in m.groups())


def test_requirements_pins_core_with_github_auth():
    txt = (ROOT / "requirements.txt").read_text()
    line = next(
        ln for ln in txt.splitlines() if ln.strip().startswith("superpos-agent-core")
    )
    assert _lower_bound(line) >= MIN_CORE, (
        "requirements.txt must require superpos-agent-core>=0.1.12 "
        "(entrypoint.sh depends on superpos_agent_core.github_auth and "
        "module_setup --skills-dir)"
    )


def test_pyproject_pins_core_with_github_auth():
    txt = (ROOT / "pyproject.toml").read_text()
    line = next(
        ln for ln in txt.splitlines() if "superpos-agent-core" in ln and ">=" in ln
    )
    assert _lower_bound(line) >= MIN_CORE, (
        "pyproject.toml must require superpos-agent-core>=0.1.12 "
        "(entrypoint.sh depends on superpos_agent_core.github_auth and "
        "module_setup --skills-dir)"
    )
