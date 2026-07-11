"""Behavioral coverage for push-and-pr.sh's gh-token minting.

The static assertions in ``test_push_and_pr.py`` pin the *shape* of the token
block; these tests actually *run* the script with a stubbed ``git``, ``gh`` and
``superpos_agent_core.github_auth`` on PATH/PYTHONPATH and assert the runtime
behavior the reviewer asked for (gilfoilbot-dev, PR #21): that the raw origin
remote — https, ssh, and a trailing-slash form — is handed to the broker via
``--repo``, and that the script falls back to the plain non-owner token when
owner-aware minting is unavailable.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (
    REPO_ROOT
    / "workspace"
    / ".codex"
    / "modules"
    / "github-pr"
    / "scripts"
    / "push-and-pr.sh"
)

# A stub `git` that answers only what push-and-pr.sh asks, so no real repo is
# needed: it reports the configured origin URL, a non-main branch, signals that
# there are staged changes (`diff --cached --quiet` -> exit 1), and no-ops the
# rest (add/commit/push).
FAKE_GIT = """#!/bin/bash
case "$1 $2" in
  "remote get-url") echo "$FAKE_ORIGIN_URL" ;;
  "rev-parse --abbrev-ref") echo "feature-branch" ;;
esac
if [ "$1" = "diff" ]; then exit 1; fi
exit 0
"""

# A stub `gh` that records the GH_TOKEN it was invoked with, closing the loop
# end-to-end (the script must have exported the minted token).
FAKE_GH = """#!/bin/bash
echo "GH_TOKEN=${GH_TOKEN:-}" > "$FAKE_GH_TOKEN_OUT"
exit 0
"""

# A stub `superpos_agent_core.github_auth` module: logs every invocation's args
# and returns an owner-scoped token for `--repo` (only when minting is enabled),
# otherwise a plain token. When FAKE_OWNER_MINT_OK is unset the `--repo` mint
# exits non-zero, simulating an older agent-core without --repo support.
FAKE_GITHUB_AUTH = '''import os
import sys

args = sys.argv[1:]
log = os.environ.get("FAKE_GH_AUTH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(" ".join(args) + "\\n")

if "--repo" in args:
    if os.environ.get("FAKE_OWNER_MINT_OK") == "1":
        print("owner-token")
        sys.exit(0)
    sys.exit(1)

print("plain-token")
sys.exit(0)
'''


def _make_env(tmp_path: Path, origin_url: str, owner_mint_ok: bool):
    """Build an isolated PATH/PYTHONPATH with the three stubs and return the
    (env, auth_log, gh_token_out) triple."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_path = bin_dir / "git"
    git_path.write_text(FAKE_GIT)
    git_path.chmod(0o755)
    gh_path = bin_dir / "gh"
    gh_path.write_text(FAKE_GH)
    gh_path.chmod(0o755)

    pkg_dir = tmp_path / "pypath" / "superpos_agent_core"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "github_auth.py").write_text(FAKE_GITHUB_AUTH)

    auth_log = tmp_path / "auth.log"
    gh_token_out = tmp_path / "gh_token.out"

    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)  # force the App / broker path
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = f"{tmp_path / 'pypath'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["FAKE_ORIGIN_URL"] = origin_url
    env["FAKE_GH_AUTH_LOG"] = str(auth_log)
    env["FAKE_GH_TOKEN_OUT"] = str(gh_token_out)
    if owner_mint_ok:
        env["FAKE_OWNER_MINT_OK"] = "1"
    return env, auth_log, gh_token_out


def _run(tmp_path: Path, env) -> subprocess.CompletedProcess:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo_dir), "Some title", "Some body"],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "origin_url",
    [
        "https://github.com/Superpos-AI/superpos-codex-agent.git",
        "git@github.com:Superpos-AI/superpos-codex-agent.git",
        "https://github.com/Superpos-AI/superpos-codex-agent.git/",  # trailing slash
    ],
    ids=["https", "ssh", "trailing-slash"],
)
def test_owner_scoped_mint_passes_raw_origin_to_broker(tmp_path, origin_url):
    """For https, ssh, and trailing-slash remotes the script hands the origin
    URL through to the broker verbatim via --repo and exports the owner-scoped
    token — parsing stays in the broker, not the shell."""
    env, auth_log, gh_token_out = _make_env(tmp_path, origin_url, owner_mint_ok=True)
    proc = _run(tmp_path, env)

    assert proc.returncode == 0, proc.stderr
    first_call = auth_log.read_text().splitlines()[0]
    assert first_call == f"token --repo {origin_url}"
    assert gh_token_out.read_text().strip() == "GH_TOKEN=owner-token"


def test_falls_back_to_plain_token_when_owner_mint_unavailable(tmp_path):
    """When owner-aware minting fails (older agent-core without --repo support)
    the script must fall back to the plain non-owner token so it never regresses
    below prior behavior."""
    env, auth_log, gh_token_out = _make_env(
        tmp_path,
        "https://github.com/Superpos-AI/superpos-codex-agent.git",
        owner_mint_ok=False,
    )
    proc = _run(tmp_path, env)

    assert proc.returncode == 0, proc.stderr
    calls = auth_log.read_text().splitlines()
    assert calls[0] == "token --repo https://github.com/Superpos-AI/superpos-codex-agent.git"
    assert calls[1] == "token"  # plain fallback, no owner/repo arg
    assert gh_token_out.read_text().strip() == "GH_TOKEN=plain-token"
