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

    def test_removed_file_is_dropped(self):
        self.r.index()
        (self.proj / "notes" / "deploy.md").unlink()
        self.assertEqual(self.r.index(), (0, 1))
        self.assertEqual(self.r.search("port", source="notes"), [])


if __name__ == "__main__":
    unittest.main()
