#!/usr/bin/env bash
# release.sh — bump version, tag, and push to trigger a CI build.
#
# Semver rules (from commits since last tag):
#   feat! / BREAKING CHANGE → major
#   feat                    → minor
#   anything else           → patch
#
# Usage:
#   ./release.sh            # auto-detect bump from commit messages
#   ./release.sh patch      # override: force patch
#   ./release.sh minor      # override: force minor
#   ./release.sh major      # override: force major

set -euo pipefail

# ── Validate working tree ─────────────────────────────────────────────────────
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree has uncommitted changes — commit or stash first" >&2
  exit 1
fi

# ── Determine bump type ───────────────────────────────────────────────────────
OVERRIDE=${1:-}

if [[ -n "$OVERRIDE" ]]; then
  if [[ "$OVERRIDE" != patch && "$OVERRIDE" != minor && "$OVERRIDE" != major ]]; then
    echo "Usage: ./release.sh [patch|minor|major]" >&2
    exit 1
  fi
  BUMP="$OVERRIDE"
else
  LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
  if [[ -n "$LAST_TAG" ]]; then
    COMMITS=$(git log "${LAST_TAG}..HEAD" --pretty=%s)
  else
    COMMITS=$(git log --pretty=%s)
  fi

  if echo "$COMMITS" | grep -qiE '^feat!|BREAKING CHANGE'; then
    BUMP=major
  elif echo "$COMMITS" | grep -qiE '^feat'; then
    BUMP=minor
  else
    BUMP=patch
  fi
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

echo "Bump: ${BUMP}  (${CURRENT} → ${NEW_VERSION})"
echo "Tag:  ${TAG}"
if [[ -z "$OVERRIDE" && -n "${LAST_TAG:-}" ]]; then
  echo ""
  echo "Commits since ${LAST_TAG}:"
  git log "${LAST_TAG}..HEAD" --pretty="  %s" | head -20
fi
echo ""
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
