"""Unit tests for verify-write core. No Hermes import, no network.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verify_write as vw  # noqa: E402


class FileWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "note.txt")

    def test_content_matches_is_verified(self):
        Path(self.path).write_text("hello", encoding="utf-8")
        entry = vw.verify_file("write_file", {"path": self.path, "content": "hello"}, "ok")
        self.assertEqual(entry.verdict, vw.VERIFIED)

    def test_silent_no_op_is_failed(self):
        # the exact failure mode: tool said ok, file holds something else
        Path(self.path).write_text("old", encoding="utf-8")
        entry = vw.verify_file("write_file", {"path": self.path, "content": "new"}, "ok")
        self.assertEqual(entry.verdict, vw.FAILED)
        self.assertIn("sha mismatch", entry.detail)

    def test_missing_file_is_failed(self):
        entry = vw.verify_file("patch", {"path": self.path}, "ok")
        self.assertEqual(entry.verdict, vw.FAILED)


class TerminalClassificationTests(unittest.TestCase):
    def test_read_only_gh_api_is_ignored(self):
        entry = vw.observe("terminal", {"command": "gh api repos/a/b"}, "ok")
        self.assertIsNone(entry)

    def test_graphql_write_is_unverified(self):
        entry = vw.verify_gh_api(
            "terminal", {"command": "gh api graphql -f query='mutation{x}'"}, "ok"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.verdict, vw.UNVERIFIED)

    def test_git_push_without_dash_c_is_unverified(self):
        entry = vw.verify_git_push("terminal", {"command": "git push origin main"}, "ok")
        self.assertEqual(entry.verdict, vw.UNVERIFIED)

    def test_adb_install_without_apk_is_unverified(self):
        entry = vw.verify_adb_install("terminal", {"command": "adb install -r"}, "ok")
        self.assertEqual(entry.verdict, vw.UNVERIFIED)

    def test_unrelated_command_is_ignored(self):
        self.assertIsNone(vw.observe("terminal", {"command": "ls -la"}, "ok"))

    def test_curl_get_is_ignored(self):
        self.assertIsNone(vw.observe("terminal", {"command": "curl -s https://example.com"}, "ok"))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        vw.reset()

    def test_summary_silent_when_clean(self):
        vw._record("s1", vw.Entry("write_file", "/tmp/a", vw.VERIFIED, "ok"))
        self.assertEqual(vw.summary("s1"), "")

    def test_summary_lists_failures_and_counts_verified(self):
        vw._record("s1", vw.Entry("write_file", "/tmp/a", vw.FAILED, "sha mismatch"))
        vw._record("s1", vw.Entry("write_file", "/tmp/b", vw.VERIFIED, "ok"))
        text = vw.summary("s1")
        self.assertIn("sha mismatch", text)
        self.assertIn("1 other write(s) verified", text)
        self.assertIn("Do not report these as done", text)

    def test_sessions_are_isolated(self):
        vw._record("s1", vw.Entry("write_file", "/tmp/a", vw.FAILED, "x"))
        self.assertEqual(vw.summary("s2"), "")

    def test_ledger_is_capped(self):
        for i in range(vw.MAX_ENTRIES_PER_SESSION + 10):
            vw._record("s1", vw.Entry("write_file", f"/tmp/{i}", vw.FAILED, "x"))
        self.assertEqual(len(vw.entries("s1")), vw.MAX_ENTRIES_PER_SESSION)


if __name__ == "__main__":
    unittest.main()
