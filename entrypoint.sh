#!/bin/bash
set -e

# Configure git identity if provided
if [ -n "$GIT_USER_NAME" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Configure GitHub auth. Centralized in agent-core so Claude and Codex stay
# in sync. Token precedence (static GITHUB_TOKEN is canonical and never expires):
#   1. GITHUB_TOKEN set → gh auth login --with-token + gh auth setup-git.
#   2. else a github_app service connection is discoverable → register a git
#      credential helper that mints short-lived installation tokens from the
#      Superpos broker on demand (no long-lived secret in the container).
#   3. else → direct git/gh auth skipped; GitHub is still reachable through the
#      Superpos proxy (the `superpos-github` module).
#
# We deliberately avoid `git config url.<token>.insteadOf`, which would bake the
# token into every clone's .git/config and leak it via `git remote -v`; both the
# gh credential helper and the broker helper hand tokens to git on demand only.
python3 -m superpos_agent_core.github_auth setup \
    || echo "Warning: GitHub auth setup failed (proxy access via superpos-github still works)"

# Disable Codex built-in GitHub plugin — it uses the OpenAI OAuth user's
# personal GitHub account instead of GITHUB_TOKEN.  All git/gh operations
# should go through the bot's GITHUB_TOKEN set above.
mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" << 'TOML'
[plugins."github@openai-curated"]
enabled = false

# Disable built-in "apps" (codex_apps) — its GitHub tools use OpenAI OAuth
# which authenticates as the OAuth account owner, not the bot's GITHUB_TOKEN.
[features]
apps = false
TOML

# Materialize OPENAI_API_KEY into ~/.codex/auth.json.
#
# `codex exec` deliberately ignores OPENAI_API_KEY from process env — it only
# reads ~/.codex/auth.json (or OAuth tokens). Without this step, setting
# OPENAI_API_KEY in the container env appears to do nothing and the agent's
# auth check fails.
#
# We skip the write if auth.json already exists so prior `codex login` OAuth
# tokens (persisted via the /home/agent/.codex volume) are preserved.
if [ -n "$OPENAI_API_KEY" ] && [ ! -f "$HOME/.codex/auth.json" ]; then
    mkdir -p "$HOME/.codex"
    cat > "$HOME/.codex/auth.json" <<EOF
{"OPENAI_API_KEY": "$OPENAI_API_KEY", "auth_mode": "apikey"}
EOF
    chmod 600 "$HOME/.codex/auth.json"
    echo "[entrypoint] Wrote $HOME/.codex/auth.json from OPENAI_API_KEY env" >&2
fi

# Run module setup: install deps, symlink scripts onto PATH, update AGENTS.md.
# --bin-dir links scripts from both workspace and core-bundled modules,
# so platform tools (e.g. superpos-issues) work even when nothing is in
# /workspace/.codex/modules.
#
# If this fails (network blip on `pip install`, broken module, etc.) the
# container still starts — the Dockerfile pre-populated modules-bin with
# build-time symlinks to workspace scripts, so those stay callable from
# PATH.  Only core-bundled tools (added at runtime) are lost in that
# degraded mode.
python3 -m superpos_agent_core.module_setup \
    --modules-dir /workspace/.codex/modules \
    --agents-md /workspace/AGENTS.md \
    --bin-dir /workspace/.codex/modules-bin \
    || echo "Warning: module setup failed (build-time workspace symlinks remain in place)"

exec "$@"
