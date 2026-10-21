import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import claude_observe  # noqa: E402


def fake_claude(tmp, project="/home/me/proj"):
    home = Path(tmp) / "claude"
    now = int(time.time() * 1000)
    hist = [
        {"display": "merge and deploy please", "timestamp": now - 1000, "project": project},
        {"display": "merge and deploy please", "timestamp": now - 2000, "project": project},
        {"display": "merge and deploy please", "timestamp": now - 2500, "project": project},
        {"display": "merge and deploy now", "timestamp": now - 3000, "project": project},
        {"display": "/model", "timestamp": now - 4000, "project": project},
        {"display": "Em là vai builder do Atlat cử tới", "timestamp": now - 5000, "project": project},
        {"display": "use key sk-ant-abcdefghijklmnopqrstuvwxyz123456 please", "timestamp": now - 6000, "project": project},
        {"display": "old prompt", "timestamp": now - 40 * 86400 * 1000, "project": project},
        {"display": "bot prompt", "timestamp": now - 7000, "project": "/home/me/bot/worktrees/T1"},
    ]
    home.mkdir(parents=True)
    (home / "history.jsonl").write_text("\n".join(json.dumps(h) for h in hist) + "\n")
    key = "-" + project.strip("/").replace("/", "-")
    pdir = home / "projects" / key
    pdir.mkdir(parents=True)
    lines = [
        {"type": "user", "cwd": project, "message": {"content": "hi"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "deploy"}}, {"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Agent", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]}},
    ]
    (pdir / "s1.jsonl").write_text("\n".join(json.dumps(l, separators=(",", ":")) for l in lines) + "\n")
    bot = home / "projects" / "-home-me-bot-worktrees-T1"
    bot.mkdir(parents=True)
    (bot / "b.jsonl").write_text(json.dumps({"type": "assistant", "cwd": "/home/me/bot/worktrees/T1", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "secret-skill"}}]}}, separators=(",", ":")) + "\n")
    for name, fm in (("deploy", "deploy"), ("idle", "idle"), ("off-one", "off")):
        d = home / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {fm}\n---\n")
    (home / "skills" / "synced").mkdir()
    (home / "settings.json").write_text(json.dumps({"skillOverrides": {"off-one": "off"}}))
    return home


class ObserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = fake_claude(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, **kw):
        return claude_observe.Observer(self.home, 14, exclude_prefix=["Em là vai"], exclude_project=["bot/worktrees"], **kw).scan()

    def test_prompts_filtered_and_grouped(self):
        o = self.scan()
        self.assertEqual(o["owner"]["total"], 6)  # 9 - old - agent prefix - bot project
        self.assertEqual(o["owner"]["commands"], {"/model": 1})
        rep = o["owner"]["repeated"]
        self.assertEqual(rep[0]["key"], "merge and deploy please")
        self.assertEqual(rep[0]["n"], 3)  # "merge and deploy now" has a different key and is not repeated
        self.assertEqual(rep[0]["project"], "/home/me/proj")

    def test_secret_redacted_and_truncated(self):
        o = self.scan()
        self.assertNotIn("sk-ant-abcdefghijklmnopqrstuvwxyz123456", json.dumps(o))

    def test_transcripts_counts(self):
        o = self.scan()
        s = o["sessions"]
        self.assertEqual(s["files"], 1)  # bot project excluded
        self.assertEqual(s["skills"], {"deploy": 1})
        self.assertEqual(s["tools"]["Bash"], 1)
        self.assertEqual(s["agent_by_project"], {"/home/me/proj": 1})
        self.assertEqual(s["tool_errors_by_project"], {"/home/me/proj": 1})
        self.assertNotIn("secret-skill", json.dumps(o))

    def test_skill_state(self):
        o = self.scan()
        self.assertEqual(o["skills"]["enabled"], ["deploy", "idle"])
        self.assertEqual(o["skills"]["off"], ["off-one"])
        self.assertEqual(o["skills"]["used_enabled"], ["deploy"])
        self.assertEqual(o["skills"]["unused"], ["idle"])

    def test_markdown_and_brief_in_both_languages(self):
        for lang in ("en", "vi"):
            ob = claude_observe.Observer(self.home, 14, ["Em là vai"], ["bot/worktrees"], lang)
            o = ob.scan()
            md = ob.markdown(o)
            self.assertIn("deploy 1", md)
            self.assertIn("1/2", ob.brief(o))

    def test_cli_json(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = claude_observe.main(["--json", "--claude-dir", str(self.home), "--exclude-project", "bot/worktrees"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["sessions"]["files"], 1)


if __name__ == "__main__":
    unittest.main()
