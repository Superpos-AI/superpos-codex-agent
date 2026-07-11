#!/bin/bash
set -euo pipefail

REPO_DIR="${1:?Usage: push-and-pr.sh <repo-dir> <pr-title> [pr-body]}"
PR_TITLE="${2:?Usage: push-and-pr.sh <repo-dir> <pr-title> [pr-body]}"
PR_BODY="${3:-}"

cd "$REPO_DIR"

# `gh` reads GITHUB_TOKEN from the env directly. When it isn't set (GitHub App
# path), mint a short-lived installation token from the Superpos broker so
# `gh pr create` is authenticated the same way `git push` is.
#
# Unlike `git` (which resolves the right connection via the owner-aware
# credential helper registered at boot), `gh` does NOT go through that helper —
# it just uses GH_TOKEN. So we must mint an OWNER-SCOPED token here: on
# multi-connection App auth, a token minted without --owner is the single boot
# token and 401s on repos owned by a different connection. Derive the owner
# from the origin remote and pass it through.
#
# Best-effort throughout: if owner extraction, the owner-scoped mint (older
# agent-core without --owner support), or the broker itself fails, GH_TOKEN
# stays empty and gh falls back to its own auth state.
if [ -z "${GITHUB_TOKEN:-}" ]; then
    ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
    OWNER="$(printf '%s' "$ORIGIN_URL" | sed -E 's#.*[:/]([^/]+)/[^/]+$#\1#')"
    if [ -n "$OWNER" ]; then
        GH_TOKEN="$(python3 -m superpos_agent_core.github_auth token --owner "$OWNER" 2>/dev/null || true)"
    fi
    if [ -z "${GH_TOKEN:-}" ]; then
        GH_TOKEN="$(python3 -m superpos_agent_core.github_auth token 2>/dev/null || true)"
    fi
    export GH_TOKEN
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
    echo "ERROR: refusing to push directly to $BRANCH"
    exit 1
fi

# Stage and commit all changes
git add -A
if git diff --cached --quiet; then
    echo "No changes to commit"
    exit 1
fi

git commit -m "$PR_TITLE"

# Push branch
git push -u origin "$BRANCH"

# Create PR
if [ -n "$PR_BODY" ]; then
    gh pr create --title "$PR_TITLE" --body "$PR_BODY"
else
    gh pr create --title "$PR_TITLE" --body ""
fi

echo "Pull request created successfully."
