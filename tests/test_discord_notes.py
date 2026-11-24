"""Check release-note formatting without sending Discord notifications."""

import subprocess
import sys
import unittest
from pathlib import Path


class DiscordNotesTests(unittest.TestCase):
    def format_notes(self, text):
        script = (
            Path(__file__).resolve().parents[1]
            / ".github/actions/discord-result/format_notes.py"
        )
        return subprocess.check_output(
            [sys.executable, str(script)], input=text, text=True
        ).strip()

    def test_changelog_sections_and_wrapped_bullets(self):
        notes = (
            "### Fixed\n\n- Publish release tags without pushing changelog\n"
            "  commits to master.\n\n\n### Docs\n\n- Update shared\n"
            "  guidance.\n- Keep [release details](https://example.com).\n"
        )
        self.assertEqual(
            self.format_notes(notes),
            "**Fixed**\n\n- Publish release tags without pushing changelog commits to master."
            "\n\n**Docs**\n\n- Update shared guidance."
            "\n- Keep [release details](https://example.com).",
        )

    def test_prose_and_numbered_lists(self):
        self.assertEqual(
            self.format_notes(
                "## Upgrade notes\n\nRestart the\ncontainer.\n\n1. Back up\n   data.\n2. Upgrade."
            ),
            "**Upgrade notes**\n\nRestart the container.\n\n1. Back up data.\n2. Upgrade.",
        )

    def test_code_and_inline_markdown_are_preserved(self):
        notes = "### Changed\n\n- Keep `AUTH_MODE` and **bold**.\n\n```sh\n### literal\n  command\n```"
        self.assertEqual(
            self.format_notes(notes), notes.replace("### Changed", "**Changed**")
        )

    def test_empty_notes(self):
        self.assertEqual(self.format_notes("\n\n"), "")
