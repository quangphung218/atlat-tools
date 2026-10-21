import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import agent_health  # noqa: E402


def proc(pid, ppid, cmd, cpu=0.0, rss=1000, tty="??", etime="00:05"):
    return {"pid": pid, "ppid": ppid, "tty": tty, "etime": etime, "rss": rss, "cpu": cpu, "cmd": cmd}


class HealthTest(unittest.TestCase):
    def test_etime(self):
        self.assertEqual(agent_health.etime_seconds("05:30"), 330)
        self.assertEqual(agent_health.etime_seconds("01:02:03"), 3723)
        self.assertEqual(agent_health.etime_seconds("2-01:00:00"), 2 * 86400 + 3600)

    def fake(self, procs, **kw):
        h = agent_health.Health(api="", **kw)
        h.procs = procs
        h.by_pid = {p["pid"]: p for p in procs}
        return h

    def test_owner_walks_up_to_the_agent_session(self):
        procs = [proc(1, 0, "launchd"), proc(10, 1, "claude --resume x", tty="ttys001"), proc(11, 10, "/bin/zsh"), proc(12, 11, "node vitest", cpu=300.0),
                 proc(20, 1, "node server.js")]
        h = self.fake(procs)
        self.assertIn("claude pid 10", h.owner(12))
        self.assertEqual(h.owner(20), "")

    def test_is_agent_matches_basename(self):
        h = self.fake([])
        self.assertTrue(h.is_agent(proc(1, 0, "/opt/bin/codex exec")))
        self.assertTrue(h.is_agent(proc(1, 0, "claude")))
        self.assertFalse(h.is_agent(proc(1, 0, "node claude-helper")))

    def test_sessions_and_stray_from_fake_procs(self):
        procs = [proc(10, 1, "claude", tty="??", rss=400_000, etime="1-00:00:00"), proc(30, 1, "node vite", etime="03:00:00"), proc(31, 1, "/Applications/Foo.app/Contents/MacOS/node")]
        h = self.fake(procs)
        sessions = h.sessions()
        self.assertEqual([s["pid"] for s in sessions], [10])
        stray, listen = h.stray()
        self.assertEqual([s["pid"] for s in stray], [30])  # app bundles are not stray
        self.assertTrue(any("re-parented" in w for w in h.warns))

    def test_brief_and_report_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            agent_health.main(["--brief", "--api", ""])
            agent_health.main(["--json", "--api", ""])
        out = buf.getvalue()
        self.assertIn("load", out)
        self.assertIn('"sessions"', out)


if __name__ == "__main__":
    unittest.main()
