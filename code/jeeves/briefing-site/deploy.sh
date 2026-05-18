#!/usr/bin/env bash
# Manual deploy helper for the Day2 Mission Control briefing site.
#
# Use this when you want to push a one-off rebuild outside the 06:25
# launchd cycle (e.g. after editing the renderer template). The daily
# auto-rebuild calls build_briefing_site.py --push directly.
#
#   bash ~/Code/jeeves/briefing-site/deploy.sh
#   bash ~/Code/jeeves/briefing-site/deploy.sh "tweaked the masthead"
#
# What it does:
#   1. cd to the repo root
#   2. Run the build script
#   3. Stage briefing-site/ changes
#   4. Commit (or skip if clean)
#   5. Push — Netlify auto-deploys

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
COMMIT_MSG="${1:-briefing-site: manual deploy $(date '+%Y-%m-%d %H:%M')}"

echo "==> Repo: $REPO_ROOT"

# Load .env if present so ANTHROPIC_API_KEY is available for the lead-story step.
# Filter to only lines that LOOK like valid KEY=VALUE pairs — a single
# malformed line (e.g. a comment without #, or a stray multi-line value)
# would otherwise blow up the whole deploy under `set -e`.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env) || true
  set +a
fi

echo "==> Building briefing-site/public/{index.html,data.json}…"
python3 scripts/build_briefing_site.py

if [[ ! -f briefing-site/public/index.html ]]; then
  echo "❌ build_briefing_site.py did not produce index.html — aborting."
  exit 1
fi

if ! git rev-parse --git-dir > /dev/null 2>&1; then
  echo "❌ Not inside a git repo. Initialise first:"
  echo "     cd $REPO_ROOT && git init && git add . && git commit -m 'initial'"
  exit 1
fi

if ! git remote get-url origin > /dev/null 2>&1; then
  echo "❌ No 'origin' remote configured for this repo."
  echo "   The health-site uses the same repo — check 'git remote -v'."
  exit 1
fi

echo "==> Staging briefing-site/…"
git add briefing-site/

if git diff --cached --quiet; then
  echo "==> Nothing to commit — everything is already up to date."
  exit 0
fi

echo "==> Committing: $COMMIT_MSG"
git commit -m "$COMMIT_MSG"

echo "==> Pushing to origin…"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" > /dev/null 2>&1; then
  git push
else
  git push --set-upstream origin "$CURRENT_BRANCH"
fi

echo ""
echo "✅ Pushed. Netlify should auto-deploy within ~30 seconds."
echo "   matt-briefing.netlify.app"
