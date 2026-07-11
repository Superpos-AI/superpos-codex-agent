import re
from pathlib import Path

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


def _read_script() -> str:
    return SCRIPT.read_text()


def _token_block() -> str:
    """Return the GH_TOKEN minting block (the `if [ -z "${GITHUB_TOKEN...`
    guard through its closing `fi`), so assertions don't match unrelated
    parts of the script."""
    text = _read_script()
    m = re.search(
        r'if \[ -z "\$\{GITHUB_TOKEN:-\}" \];.*?\nfi',
        text,
        re.DOTALL,
    )
    assert m, "could not locate the GITHUB_TOKEN minting block in push-and-pr.sh"
    return m.group(0)


def test_owner_aware_mint_uses_repo_arg():
    """The owner-aware mint must pass the raw origin URL to the broker via
    --repo. The broker parses the owner centrally (handling https/ssh/slug
    forms and a trailing slash); passing the URL as-is is what makes gh
    authenticate against the *right* GitHub App connection on multi-connection
    agents."""
    block = _token_block()
    assert re.search(
        r'github_auth token --repo "\$ORIGIN_URL"', block
    ), "owner-aware mint must delegate parsing to the broker via --repo \"$ORIGIN_URL\""


def test_no_fragile_local_owner_regex():
    """Regression for the trailing-slash bug (gilfoilbot-dev, PR #21): a local
    `sed`/regex owner extraction mis-parses a valid trailing-slash remote such
    as https://github.com/org/repo.git/ (leaving OWNER as the full URL), so the
    owner-scoped mint fails and the plain-token fallback can hand back the
    unscoped boot token — a 401 on a different connection. Owner parsing must
    stay in the broker, not a brittle shell regex."""
    block = _token_block()
    assert "--owner" not in block, (
        "must not extract the owner locally and pass --owner; delegate URL "
        "parsing to the broker via --repo instead"
    )
    assert "sed" not in block, (
        "must not parse the origin URL with a local sed/regex; the broker "
        "parses the owner from --repo"
    )


def test_plain_token_fallback_retained():
    """When owner-aware minting is unavailable (older agent-core without --repo,
    no origin URL, or a broker failure), the script must still fall back to the
    plain non-owner token so it never regresses below prior behavior."""
    block = _token_block()
    assert re.search(
        r'if \[ -z "\$\{GH_TOKEN:-\}" \];[^\n]*\n\s*'
        r"GH_TOKEN=\"\$\(python3 -m superpos_agent_core\.github_auth token "
        r'2>/dev/null \|\| true\)"',
        block,
    ), "plain-token fallback (github_auth token, no owner arg) must be retained"
