# Releasing

This repo uses Conventional Commits style messages and auto-tags releases from the
latest commit on `master`.

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

