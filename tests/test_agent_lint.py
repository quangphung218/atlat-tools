import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import agent_lint  # noqa: E402


def write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class AgentLintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.home = Path(self.tmp.name) / "claude"
        (self.home / "skills").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def lint(self, **kw):
        lint = agent_lint.Linter(home_claude=self.home, **kw)
        lint.root(self.root, "proj")
        return lint

    def messages(self, lint):
        return [m for _, _, _, m in lint.items]

    def test_clean_project_has_no_findings(self):
        write(self.root / "CLAUDE.md", "# Project\n\nRead `docs/guide.md` first.\n")
        write(self.root / "docs" / "guide.md", "guide")
        write(self.root / ".claude" / "settings.json", json.dumps({"permissions": {"allow": ["Bash(npm test)"]}}))
        write(self.root / ".claude" / "skills" / "deploy" / "SKILL.md", "---\nname: deploy\ndescription: deploy it\n---\n# Deploy\n")
        lint = self.lint()
        self.assertEqual(lint.items, [])
        self.assertEqual(lint.counts(), {"error": 0, "warn": 0, "info": 0})

    def test_dead_import_and_path(self):
        write(self.root / "CLAUDE.md", "@rules/missing.md\n\nSee `docs/nope.md` and `docs/here.md`.\n")
        write(self.root / "docs" / "here.md", "x")
        lint = self.lint()
        levels = {m: lv for lv, _, _, m in lint.items}
        self.assertTrue(any("@rules/missing.md" in m and levels[m] == "error" for m in levels))
        self.assertTrue(any("docs/nope.md" in m and levels[m] == "warn" for m in levels))
        self.assertFalse(any("docs/here.md" in m for m in levels))

    def test_template_folder_only_notes(self):
        write(self.root / "templates" / "CLAUDE.md", "@generated/rules.md\n")
        lint = self.lint()
        self.assertEqual([lv for lv, *_ in lint.items], ["info"])

    def test_settings_checks(self):
        write(self.root / ".claude" / "settings.json", json.dumps({
            "permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash", "Bash(rm -rf *)"], "deny": ["Read(**/.env)"]},
            "hooks": {"PreToolUse": [{"hooks": [{"command": "/no/such/hook.sh"}]}]},
        }))
        msgs = self.messages(self.lint())
        self.assertTrue(any("bypassPermissions" in m for m in msgs))
        self.assertTrue(any("too broad" in m for m in msgs))
        self.assertTrue(any("rm -rf" in m for m in msgs))
        self.assertTrue(any("//**/" in m for m in msgs))
        self.assertTrue(any("/no/such/hook.sh" in m for m in msgs))

    def test_broken_json_is_an_error(self):
        write(self.root / ".claude" / "settings.json", "{not json")
        lint = self.lint()
        self.assertEqual(lint.counts()["error"], 1)

    def test_skill_problems(self):
        write(self.root / ".claude" / "skills" / "nofm" / "SKILL.md", "# no frontmatter\n")
        write(self.root / ".claude" / "skills" / "noname" / "SKILL.md", "---\ndescription: x\n---\n")
        (self.root / ".claude" / "skills" / "empty").mkdir(parents=True)
        write(self.root / ".claude" / "skills" / "group" / "inner" / "SKILL.md", "---\nname: inner\ndescription: d\n---\n")
        write(self.home / "skills" / "dup" / "SKILL.md", "---\nname: dup\ndescription: d\n---\n")
        write(self.root / ".claude" / "skills" / "dup" / "SKILL.md", "---\nname: dup\ndescription: d\n---\n")
        msgs = self.messages(self.lint())
        self.assertTrue(any("no frontmatter" in m for m in msgs))
        self.assertTrue(any("missing `name`" in m for m in msgs))
        self.assertTrue(any("without SKILL.md" in m for m in msgs))
        self.assertTrue(any("nested" in m for m in msgs))
        self.assertTrue(any("global skill" in m for m in msgs))

    def test_secret_is_masked(self):
        write(self.root / "CLAUDE.md", "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n")
        lint = self.lint()
        self.assertEqual(lint.counts()["error"], 1)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", lint.items[0][3])

    def test_mcp_missing_command(self):
        write(self.root / ".mcp.json", json.dumps({"mcpServers": {"x": {"command": "definitely-not-installed-xyz"}}}))
        msgs = self.messages(self.lint())
        self.assertTrue(any("MCP `x`" in m for m in msgs))

    def test_agents_skills_not_loaded(self):
        write(self.root / ".agents" / "skills" / "a" / "SKILL.md", "---\nname: a\ndescription: d\n---\n")
        msgs = self.messages(self.lint())
        self.assertTrue(any(".agents/skills" in m for m in msgs))

    def test_vietnamese_messages_and_json(self):
        write(self.root / "CLAUDE.md", "@missing.md\n")
        lint = self.lint(lang="vi")
        self.assertIn("không tồn tại", lint.items[0][3])
        j = lint.json(1)
        self.assertEqual(j["counts"]["error"], 1)
        self.assertEqual(j["findings"][0]["level"], "error")

    def test_cli_exit_code(self):
        write(self.root / "CLAUDE.md", "@missing.md\n")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.home)
        try:
            self.assertEqual(agent_lint.main([str(self.root), "--quiet"]), 1)
            write(self.root / "CLAUDE.md", "ok\n")
            self.assertEqual(agent_lint.main([str(self.root), "--quiet"]), 0)
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]


if __name__ == "__main__":
    unittest.main()
