"""Regression coverage for curated release publication."""

import unittest

from scripts.changelog import finalize, release_notes


class ChangelogTests(unittest.TestCase):
    def test_finalize_preserves_history_and_extracts_only_current_release(self):
        text = (
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n\n- Restore login.\n\n"
            "## [v1.0.0] - 2026-01-01\n\n### Added\n\n- First release.\n"
        )
        result = finalize(text, "v1.0.1", "2026-11-24")
        self.assertIn("## [Unreleased]\n\n## [v1.0.1] - 2026-11-24", result)
        self.assertEqual(
            release_notes(result, "v1.0.1"), "### Fixed\n\n- Restore login.\n"
        )
        self.assertEqual(
            release_notes(result, "v1.0.0"), "### Added\n\n- First release.\n"
        )
        with self.assertRaises(ValueError):
            finalize(result, "v1.0.2", "2026-11-24")
        with self.assertRaises(ValueError):
            finalize(result, "v1.0.1", "2026-11-24")

    def test_missing_or_empty_notes_fail(self):
        for text in ["# Changelog\n", "## [Unreleased]\n\n### Fixed\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                finalize(text, "v1.0.1", "2026-11-24")
        with self.assertRaises(ValueError):
            release_notes("## [v1.0.10] - 2026-01-01\n- Other release.\n", "v1.0.1")

    def test_invalid_tag_fails(self):
        with self.assertRaises(ValueError):
            finalize("## [Unreleased]\n- Fix.\n", "invalid", "2026-11-24")
