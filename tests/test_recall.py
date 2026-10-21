import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import recall  # noqa: E402


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.proj = base / "proj"
        (self.proj / "notes" / "node_modules").mkdir(parents=True)
        (self.proj / "notes" / "deploy.md").write_text("# Triển khai\n\nDeploy failed because the port was busy.\n\nSecond paragraph.\n")
        (self.proj / "notes" / "node_modules" / "x.md").write_text("ignored")
        self.claude = base / "claude"
        key = recall.project_key(self.proj)
        pdir = self.claude / "projects" / key
        (pdir / "memory").mkdir(parents=True)
        (pdir / "memory" / "MEMORY.md").write_text("- remember: canary passed 10/10\n")
        lines = [
            {"type": "user", "timestamp": "2026-10-20T03:00:00.000Z", "message": {"content": "why did the deploy fail?"}},
            {"type": "assistant", "timestamp": "2026-10-20T03:00:05.000Z", "message": {"content": [{"type": "text", "text": "The port was busy. <system-reminder>hidden</system-reminder>"}, {"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "isSidechain": True, "timestamp": "2026-10-20T03:00:06.000Z", "message": {"content": "sidechain noise"}},
        ]
        (pdir / "s.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        self.db = base / "r.db"
        self.r = recall.Recall(self.proj, self.db, [self.proj], self.claude)

    def tearDown(self):
        self.r.db.close()
        self.tmp.cleanup()

    def test_index_and_search(self):
        changed, gone = self.r.index()
        self.assertEqual((changed, gone), (3, 0))
        self.assertEqual(self.r.index(), (0, 0))  # unchanged files are not re-read
        hits = self.r.search("port busy")
        self.assertEqual({h[1] for h in hits}, {"chat", "notes"})
        self.assertTrue(any(h[2] == "assistant" for h in hits))
        self.assertFalse(any("hidden" in h[4] or "sidechain" in h[4] for h in hits))
        self.assertEqual(len(self.r.search("port", source="memory")), 0)
        self.assertEqual(len(self.r.search("canary", source="memory")), 1)

    def test_diacritics_insensitive(self):
        self.r.index()
        self.assertTrue(self.r.search("trien khai"))
        self.assertTrue(self.r.search("triển khai"))

    def test_odd_query_characters_fall_back_to_phrases(self):
        self.r.index()
        self.assertTrue(self.r.search("port-was busy."))

    def test_labelled_dirs(self):
        r = recall.Recall(self.proj, Path(self.tmp.name) / "l.db", [f"{self.proj / 'notes'}:runbook"], self.claude)
        r.index()
        self.assertEqual({h[1] for h in r.search("port busy", source="runbook")}, {"runbook"})
        r.db.close()

    def test_secrets_are_redacted_and_ignore_file_respected(self):
        (self.proj / "notes" / "keys.md").write_text("api_key = abcdefghij1234567890\nnote about keys\n")
        (self.proj / "notes" / "course.md").write_text("Key google studio\nAQ.Ab8RN6Kf2wzAm7liMzlHbv469TCXNrmuumW3NZO9NC\nkey AIzaSyA1234567890abcdefghijklmnopqrstuvw\nthe token is zq9X8v7W6u5T4s3R2q1P0o9N8m7L\n")
        (self.proj / "notes" / "accounts.md").write_text("user: a@b.com\npass: Secret1234\nmk: Abc@6868\nchatgpt: x@y.com|Hunter22!\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
        (self.proj / "notes" / "private.md").write_text("do not index this\n")
        (self.proj / ".recallignore").write_text("# test\nnotes/private.md\n")
        changed, _ = self.r.index()
        self.assertGreater(self.r.redacted, 0)
        self.assertEqual(self.r.search("abcdefghij1234567890"), [])
        self.assertTrue(self.r.search("redacted", source="notes"))      # keys.md: one masked span, rest indexed
        self.assertTrue(any(p.endswith("accounts.md") for p in self.r.secret_files))
        for secret in ("Secret1234", "Abc@6868", "Hunter22", "AAAA", "Ab8RN6Kf2wzAm7liMzlHbv469TCXNrmuumW3NZO9NC", "AIzaSyA1234567890abcdefghijklmnopqrstuvw", "zq9X8v7W6u5T4s3R2q1P0o9N8m7L"):
            self.assertEqual(self.r.search(secret), [], secret)
        self.assertEqual(self.r.search("a@b.com"), [])                    # credential note is not indexed at all
        self.assertEqual(self.r.index(), (0, 0))                          # and not re-read next time
        self.assertEqual(self.r.search("index this"), [])

    def test_removed_file_is_dropped(self):
        self.r.index()
        (self.proj / "notes" / "deploy.md").unlink()
        self.assertEqual(self.r.index(), (0, 1))
        self.assertEqual(self.r.search("port", source="notes"), [])


if __name__ == "__main__":
    unittest.main()
