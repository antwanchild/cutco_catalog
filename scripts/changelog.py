"""Finalize curated changelog entries and extract release notes."""

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path


def release_notes(text: str, tag: str) -> str:
    """Extract the notes for an exact release tag."""
    match = re.search(
        rf"^## \[{re.escape(tag)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if match is None or not re.search(r"^- \S", match.group(1), re.MULTILINE):
        raise ValueError(f"No curated notes found for {tag}")
    return match.group(1).strip() + "\n"


def finalize(text: str, tag: str, date: str) -> str:
    """Move Unreleased entries into a dated release section."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a version tag such as v1.2.3")
    if re.search(rf"^## \[{re.escape(tag)}\]", text, re.MULTILINE):
        raise ValueError(f"Release {tag} already exists in the changelog")
    match = re.search(
        r"^## \[Unreleased\]\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if match is None or not re.search(r"^- \S", match.group(1), re.MULTILINE):
        raise ValueError("Add at least one Unreleased changelog entry before releasing")
    return (
        text[: match.start()]
        + f"## [Unreleased]\n\n## [{tag}] - {date}\n\n"
        + match.group(1).strip()
        + "\n\n"
        + text[match.end() :]
    ).rstrip() + "\n"


def main() -> None:
    """Run changelog finalization or release-note extraction."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["finalize", "notes"])
    parser.add_argument("tag")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = args.changelog.read_text(encoding="utf-8")
    try:
        if args.action == "finalize":
            date = datetime.now(timezone.utc).date().isoformat()
            args.changelog.write_text(finalize(text, args.tag, date), encoding="utf-8")
        elif args.output:
            args.output.write_text(release_notes(text, args.tag), encoding="utf-8")
        else:
            print(release_notes(text, args.tag), end="")
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
