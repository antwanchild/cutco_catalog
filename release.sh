#!/usr/bin/env bash
# release.sh — bump version, tag, and push to trigger a CI build.
#
# Usage:
#   ./release.sh patch     # 1.2.3 → 1.2.4
#   ./release.sh minor     # 1.2.3 → 1.3.0
#   ./release.sh major     # 1.2.3 → 2.0.0

set -euo pipefail

BUMP=${1:-}

if [[ "$BUMP" != patch && "$BUMP" != minor && "$BUMP" != major ]]; then
  echo "Usage: ./release.sh patch|minor|major" >&2
  exit 1
fi

# ── Validate working tree ─────────────────────────────────────────────────────
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree has uncommitted changes — commit or stash first" >&2
  exit 1
fi

# ── Calculate new version ─────────────────────────────────────────────────────
CURRENT=$(cat VERSION)
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"

case "$BUMP" in
  major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
  minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
  patch) PATCH=$((PATCH + 1)) ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
TAG="v${NEW_VERSION}"

echo "Bumping ${CURRENT} → ${NEW_VERSION}"
read -r -p "Proceed? [y/N] " CONFIRM
if [[ "$CONFIRM" != y && "$CONFIRM" != Y ]]; then
  echo "Aborted."
  exit 0
fi

# ── Update files ──────────────────────────────────────────────────────────────
echo "$NEW_VERSION" > VERSION
sed -i.bak "s|version-[0-9]*\.[0-9]*\.[0-9]*-blue|version-${NEW_VERSION}-blue|" README.md
rm -f README.md.bak

# ── Commit, tag, push ─────────────────────────────────────────────────────────
git add VERSION README.md
git commit -m "chore: release ${NEW_VERSION}"
git push

git tag "$TAG"
git push origin "$TAG"

echo "Released ${TAG} — CI build triggered."
