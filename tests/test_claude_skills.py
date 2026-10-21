import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import claude_skills  # noqa: E402


def fake_home(tmp):
    home = Path(tmp) / "claude"
    for name, text in (
        ("deploy", "---\nname: deploy\ndescription: deploy\n---\n# Deploy\n"),
        ("idle", "---\nname: idle\ndescription: idle\n---\nYou MUST do this.\n"),
        ("nofm", "# No frontmatter\n\nbody\n"),
        ("off-one", "---\nname: off-one\ndescription: x\n---\n"),
    ):
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text)
    (home / "skills" / "synced").mkdir()
    (home / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "skillOverrides": {"off-one": "off"}}, indent=2))
    return home


USAGE = {"days": 14, "skills": {"used": {"deploy": 3}}, "owner": {"repeated": [{"n": 6, "key": "merge and deploy", "example": "merge and deploy please", "project": "p"}]}}


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = fake_home(self.tmp.name)
        self.s = claude_skills.Skills(self.home, "en", 14, USAGE)
        self.s.lock_file = Path(self.tmp.name) / "no-lock.json"  # no npx skills in the fixture
        self.s.cache = Path(self.tmp.name) / "upstream.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_inventory(self):
        rows, days = self.s.inventory()
        self.assertEqual(days, 14)
        by = {r["name"]: r for r in rows}
        self.assertEqual(set(by), {"deploy", "idle", "nofm", "off-one"})
        self.assertEqual(by["deploy"]["used"], 3)
        self.assertTrue(by["deploy"]["enabled"])
        self.assertFalse(by["off-one"]["enabled"])
        self.assertIn("no frontmatter", by["nofm"]["lint"])
        self.assertTrue(any("MUST" in x for x in by["idle"]["lint"]))

    def test_disable_enable_touch_only_overrides(self):
        before = json.loads((self.home / "settings.json").read_text())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.s.set_overrides(["idle", "missing"], "off", "test"), ["idle"])
        after = json.loads((self.home / "settings.json").read_text())
        self.assertEqual(after["permissions"], before["permissions"])
        self.assertEqual(after["skillOverrides"], {"off-one": "off", "idle": "off"})
        self.assertTrue(list((self.home / "backups" / "skills").glob("*-settings/settings.json")))
        self.assertIn("disable idle", (self.home / "skills.log").read_text())
        with redirect_stdout(io.StringIO()):
            self.s.set_overrides(["idle"], "on", "test")
        self.assertEqual(json.loads((self.home / "settings.json").read_text())["skillOverrides"], {"off-one": "off"})

    def test_patch_adds_frontmatter_with_backup(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.s.patch(False), [])
            self.assertEqual(self.s.patch(True), ["nofm"])
        text = (self.home / "skills" / "nofm" / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: nofm\ndescription: No frontmatter\n---\n"))
        self.assertTrue(list((self.home / "backups" / "skills").glob("*-skill-nofm/SKILL.md")))

    def test_propose(self):
        rows, days = self.s.inventory()
        props = self.s.propose(rows, days, USAGE["owner"]["repeated"])
        kinds = {(p["kind"], p["name"]) for p in props}
        self.assertIn(("disable", "idle"), kinds)
        self.assertIn(("patch", "nofm"), kinds)
        self.assertIn(("soften", "idle"), kinds)
        self.assertIn(("new", "merge and deploy"), kinds)
        self.assertNotIn(("disable", "deploy"), kinds)
        self.assertFalse(any(p["kind"] == "disable" and p["auto"] for p in props))

    def test_cli_unused_json(self):
        usage = Path(self.tmp.name) / "usage.json"
        usage.write_text(json.dumps(USAGE))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = claude_skills.main(["unused", "--json", "--claude-dir", str(self.home), "--usage", str(usage)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), ["idle", "nofm"])


if __name__ == "__main__":
    unittest.main()
