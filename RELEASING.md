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
