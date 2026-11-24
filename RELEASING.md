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

The release job keeps the existing version calculation. Before tagging, it:

1. Requires at least one Unreleased bullet.
2. Moves those entries into a section named `## [vX.Y.Z] - YYYY-MM-DD` using
   the UTC release date, leaving a fresh Unreleased section.
3. Commits that changelog update and atomically pushes the commit to `master`
   with its release tag. A concurrent branch update causes the push to fail
   rather than publishing a tag with an unrecorded changelog update.
4. Builds the tagged commit and uses its version section as the GitHub release
   body. Existing releases are left untouched on reruns.

The workflow token must be allowed to push the changelog commit to `master`;
branch protection must permit that workflow if protection is enabled. Pushes
using `GITHUB_TOKEN` do not trigger another release workflow run. If a concurrent
push rejects publication, rerun from the latest master commit.

To preview finalization without modifying the repository changelog:

```bash
cp CHANGELOG.md /tmp/cutco-changelog-preview.md
python3 scripts/changelog.py finalize v999.0.0 --changelog /tmp/cutco-changelog-preview.md
python3 scripts/changelog.py notes v999.0.0 --changelog /tmp/cutco-changelog-preview.md
```

Historical repair tags without the changelog tooling retain GitHub-generated
notes. The changelog includes a selective historical backfill verified against published
GitHub releases and their associated PRs. Dates use the UTC publication date.
Omitted versions remain available in GitHub Releases. When extending the backfill,
verify versions and dates, link the original release, and avoid inferring release
contents from commit titles alone.
