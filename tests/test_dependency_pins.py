import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# github_auth landed in superpos-agent-core 0.1.2; entrypoint.sh invokes
# `python3 -m superpos_agent_core.github_auth setup`, so a build that resolves
# an older core silently leaves git/gh auth unconfigured.
MIN_CORE = (0, 1, 2)


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
        "requirements.txt must require superpos-agent-core>=0.1.2 "
        "(entrypoint.sh depends on superpos_agent_core.github_auth)"
    )


def test_pyproject_pins_core_with_github_auth():
    txt = (ROOT / "pyproject.toml").read_text()
    line = next(
        ln for ln in txt.splitlines() if "superpos-agent-core" in ln and ">=" in ln
    )
    assert _lower_bound(line) >= MIN_CORE, (
        "pyproject.toml must require superpos-agent-core>=0.1.2 "
        "(entrypoint.sh depends on superpos_agent_core.github_auth)"
    )
