"""Exercise release tagging against an isolated protected remote."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    shutil.which("git") and shutil.which("bash"), "Requires git and bash"
)
class ReleaseTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
            "RELEASE_COMMIT_MSG": "fix: release safely",
        }
        self.git("init", "--initial-branch=master")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")
        (self.repo / "seed").write_text("seed\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.git("tag", "v1.0.0")
        (self.repo / "scripts").mkdir()
        source = Path(__file__).resolve().parents[1] / "scripts"
        for name in ("release_tag.sh", "changelog.py"):
            shutil.copy(source / name, self.repo / "scripts" / name)
        (self.repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [v1.0.1] - 2026-11-24\n\n### Fixed\n\n- Release safely.\n"
        )
        self.git("add", ".")
        self.git("commit", "-m", "fix: release safely")
        self.approved_sha = self.git("rev-parse", "HEAD")
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "master", "--tags")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nwhile read old new ref; do\n"
            'case "$ref" in refs/heads/*) echo "Protected branch" >&2; exit 1;; esac\n'
            "done\n"
        )
        hook.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()

    def release(self, *args):
        return subprocess.run(
            ["bash", "scripts/release_tag.sh", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_publish_tags_approved_commit_without_touching_protected_branch(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.approved_sha)
        self.assertEqual(self.git("rev-parse", "v1.0.1"), self.approved_sha)
        self.assertIn(
            self.approved_sha, self.git("ls-remote", "origin", "refs/tags/v1.0.1")
        )
        self.assertEqual(self.git("status", "--porcelain"), "")
        # The remote really rejects branch writes, while accepting release tags.
        blocked = subprocess.run(
            ["git", "push", "origin", "HEAD:refs/heads/blocked"],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(blocked.returncode, 0)
        rerun = self.release()
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("rerun=true", (self.root / "output").read_text())

    def test_check_does_not_create_tags_or_modify_checkout(self):
        result = self.release("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("tag", "--list", "v1.0.1"), "")
        self.assertEqual(self.git("ls-remote", "origin", "refs/tags/v1.0.1"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_missing_notes_fail_before_creating_tag(self):
        (self.repo / "CHANGELOG.md").write_text("## [Unreleased]\n- Pending.\n")
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Finalize CHANGELOG.md for v1.0.1", result.stderr)
        self.assertEqual(self.git("tag", "--list", "v1.0.1"), "")
        self.assertEqual(self.git("ls-remote", "origin", "refs/tags/v1.0.1"), "")
