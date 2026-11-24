# Releasing

This repo uses Conventional Commits style messages and auto-tags releases from the
latest non-documentation-only commit on `master`. A Markdown-only push does not
create a release or rebuild the container image; the manual release-repair workflow
remains available when needed.

## Version Bumps

The release workflow follows this rule:

- `feat:` -> minor release
- `fix:` -> patch release
- `feat!:` or `BREAKING CHANGE:` -> major release
- anything else -> patch release

That matches the Conventional Commits and SemVer guidance used by the CI workflow.

## What Counts As `feat`

Use `feat` when you add a new user-facing capability or workflow, for example:

- a new page
- a new browse view
- a new import or sync flow
- a new dashboard shortcut

## What Stays As `fix`

Use `fix` when you correct behavior, handle edge cases, or make the existing flow
more accurate without adding a new capability.

## What Stays Out Of Version Bumps

Use `docs`, `style`, `test`, or `refactor` for:

- copy updates
- layout polish
- test-only changes
- internal cleanup that does not change behavior

## Practical Rule Of Thumb

If a user can do something they could not do before, consider `feat`.
If the app just does the same thing more correctly, consider `fix`.

## Quick Check Before Pushing

- Make sure the commit message type matches the change.
- If you expect a new release version, use a `feat:` commit message.
- If the change is only a bug fix or cleanup, keep it as `fix:` or another non-feature type.

## Authentication-breaking releases

When an authentication change removes a supported login or setup path, use
`feat!:` and include an upgrade note. The current setup model uses a one-time
`INITIAL_SETUP_TOKEN` only to create the first local administrator at `/setup`.
It never grants an administrator session. Before deploying the breaking removal
of `ADMIN_TOKEN`, ensure an existing installation has a working named local or
proxy administrator; remove `ADMIN_TOKEN` after deployment. For a fresh local or
hybrid installation, set a long random `INITIAL_SETUP_TOKEN`, complete `/setup`,
then remove that value. Roll back the container image if account access has not
been verified; the user database is additive and does not need to be recreated.

## Curated changelog and release notes

Update `CHANGELOG.md` under `## [Unreleased]` in each release-producing PR.
Describe the effect on users in plain language and link relevant issues or PRs
when helpful. Use only headings that have entries: `Added`, `Changed`,
`Deprecated`, `Removed`, `Fixed`, `Security`, and optionally `Docs`. Internal
cleanup does not need an entry unless it affects installation or operation.
Dependency updates that trigger a release should briefly describe the update.
Markdown-only changes can accumulate under Unreleased until the next release.

Put required upgrade steps alongside the relevant change. For authentication,
configuration, or database changes, explain what an existing installation must
do and any rollback limitations. Do not use commit titles as a substitute for
release notes.

Release preparation happens in the reviewed PR; the release job never commits
or pushes to `master`. After updating Unreleased:

1. Fetch current tags (`git fetch origin master --tags`) and determine the next
   version using the intended squash commit title and the version bump rules above.
2. Run `python3 scripts/changelog.py finalize vX.Y.Z` to move Unreleased into a
   dated version section (UTC preparation date). Commit the result in the PR.
3. Validate without creating tags or pushing:
   `RELEASE_COMMIT_MSG="fix: your intended squash title" bash scripts/release_tag.sh --check`.
4. Merge the PR with that squash title after checks pass. If another release merges
   first, update the pending section's version and revalidate before merging.

The release workflow calculates the version as before, requires a matching version
section containing at least one bullet, then tags the approved commit and pushes
only that tag. Missing notes fail before tag creation or image publication. The
image and GitHub release notes come from the tagged commit. Existing releases are
left untouched on reruns; branch protection stays enabled without a bot bypass.
Discord notifications format Markdown headings as bold section labels and join
soft-wrapped prose and bullets for embed display. GitHub release notes retain
the original Markdown.
The `Changelog` PR workflow validates the intended squash title against current
tags for PRs containing non-Markdown changes. Changing the title reruns that check.

Markdown-only PRs may leave entries in Unreleased until the next release-producing
PR. Do not run `scripts/release_tag.sh` without `--check` for local validation.
A failed run from before this fix still uses the old script; merge this fix to
start a new release run rather than rerunning the old commit.

Historical repair tags without the changelog tooling retain GitHub-generated
notes. The changelog includes a selective historical backfill verified against published
GitHub releases and their associated PRs. Dates use the UTC publication date.
Omitted versions remain available in GitHub Releases. When extending the backfill,
verify versions and dates, link the original release, and avoid inferring release
contents from commit titles alone.
