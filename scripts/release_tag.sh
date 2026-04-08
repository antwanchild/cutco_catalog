#!/usr/bin/env bash
set -euo pipefail

COMMIT_MSG=$(git log -1 --pretty=%B)
LAST_TAG=$(git tag --list 'v[0-9]*' --sort=-version:refname | head -1)
LAST_TAG=${LAST_TAG:-v0.0.0}
VERSION=${LAST_TAG#v}
IFS='.' read -r MAJOR MINOR PATCH <<< "$VERSION"

if echo "$COMMIT_MSG" | grep -qiE '^feat!|BREAKING CHANGE'; then
  MAJOR=$((MAJOR + 1))
  MINOR=0
  PATCH=0
elif echo "$COMMIT_MSG" | grep -qiE '^feat'; then
  MINOR=$((MINOR + 1))
  PATCH=0
else
  PATCH=$((PATCH + 1))
fi

NEW_TAG="v${MAJOR}.${MINOR}.${PATCH}"
echo "Tagging as ${NEW_TAG}"

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

if git rev-parse -q --verify "refs/tags/${NEW_TAG}" >/dev/null; then
  TAG_COMMIT=$(git rev-list -n 1 "$NEW_TAG")
  if [ "$TAG_COMMIT" != "$(git rev-parse HEAD)" ]; then
    echo "Tag ${NEW_TAG} already exists on ${TAG_COMMIT}, refusing to retag a different commit." >&2
    exit 1
  fi
  echo "Tag ${NEW_TAG} already exists on this commit; skipping tag creation."
  RERUN=true
else
  git tag "$NEW_TAG"
  git push origin "$NEW_TAG"
  RERUN=false
fi

{
  echo "new_tag=${NEW_TAG}"
  echo "version=${MAJOR}.${MINOR}.${PATCH}"
  echo "rerun=${RERUN}"
} >> "$GITHUB_OUTPUT"

{
  echo "## Release Tag"
  echo ""
  echo "- Commit message: ${COMMIT_MSG}"
  echo "- Previous tag: ${LAST_TAG}"
  echo "- Computed tag: ${NEW_TAG}"
  echo "- Rerun: $( [ \"$RERUN\" = "true" ] && echo yes || echo no )"
} >> "$GITHUB_STEP_SUMMARY"
