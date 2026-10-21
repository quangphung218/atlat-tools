import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import logexpect  # noqa: E402


def iso(dt):
    return dt.isoformat()


class LogexpectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "app.log"
        self.log.write_text("")
        self.x = logexpect.Expect(Path(self.tmp.name) / "state.json", follow_hours=2, stale_days=7)

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, *lines):
        with self.log.open("a") as f:
            for l in lines:
                f.write(l + "\n")

    def test_parse_ts(self):
        now = datetime.now(timezone.utc)
        t, d = logexpect.parse_ts('{"ts":"2026-10-20T03:00:00.000Z","event":"x","task":7}', now)
        self.assertEqual(t.year, 2026)
        self.assertEqual(d["task"], 7)
        t, d = logexpect.parse_ts("2026-10-20 03:00:00 plain text", now)
        self.assertEqual((t.hour, d), (3, None))
        t, d = logexpect.parse_ts("no timestamp here", now)
        self.assertEqual((t, d), (now, None))
        t, _ = logexpect.parse_ts('{"time": 1792500000000, "event": "x"}', now)
        self.assertEqual(t.year, 2026)

    def test_ok_only_for_the_same_correlation_id(self):
        self.x.add("rerun", '"event":"rerun"', self.log, ok='"event":"rerun_done"', bad='"event":"rerun_failed"')
        self.assertEqual(self.x.check(), [])
        t = datetime.now(timezone.utc) + timedelta(seconds=1)
        compact = lambda d: json.dumps(d, separators=(",", ":"))  # the regexes below assume compact JSON, as most loggers write
        self.append(compact({"ts": iso(t), "event": "rerun", "task": 7}),
                    compact({"ts": iso(t + timedelta(seconds=1)), "event": "rerun_failed", "task": 8}),
                    compact({"ts": iso(t + timedelta(seconds=2)), "event": "rerun_done", "task": 7}))
        fresh = self.x.check()
        self.assertEqual(len(fresh), 1)
        self.assertIn("✅", fresh[0])
        self.assertEqual(self.x.load()[0]["result"]["kind"], "ok")

    def test_bad_wins_and_plain_text_seen(self):
        self.x.add("job", "STARTED", self.log, ok="DONE", bad="FAILED")
        self.x.add("plain", "hello", self.log)
        t = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.append(f"{iso(t)} STARTED job", f"{iso(t + timedelta(seconds=1))} FAILED job", f"{iso(t + timedelta(seconds=2))} DONE job", "hello world")
        kinds = {e["name"]: e["result"]["kind"] for e in (self.x.check(), self.x.load())[1]}
        self.assertEqual(kinds, {"job": "bad", "plain": "seen"})

    def test_unclear_after_follow_window(self):
        self.x.follow = timedelta(seconds=0)
        self.x.add("slow", "BEGIN", self.log, ok="END")
        self.append(f"{iso(datetime.now(timezone.utc))} BEGIN")  # hit is in the past by the time check() runs
        self.x.check()
        self.assertEqual(self.x.load()[0]["result"]["kind"], "unclear")

    def test_stale_and_prune_and_replace(self):
        self.x.add("never", "NOPE", self.log)
        items = self.x.load()
        items[0]["since"] = iso(datetime.now(timezone.utc) - timedelta(days=8))
        self.x.save(items)
        fresh = self.x.check()
        self.assertEqual(len(fresh), 1)
        self.assertTrue(self.x.load()[0]["stale"])
        self.assertEqual(self.x.check(), [])  # reported once
        i, replaced = self.x.add("again", "A", self.log)
        i2, replaced2 = self.x.add("again", "B", self.log)
        self.assertEqual((i, replaced, i2, replaced2), (1, False, 1, True))
        n, left = self.x.prune()
        self.assertEqual((n, left), (1, 1))

    def test_cli_requires_log_for_add(self):
        state = Path(self.tmp.name) / "s.json"
        rc = logexpect.main(["add", "x", "y", "--log", str(self.log), "--state", str(state)])
        self.assertEqual(rc, 0)
        self.assertEqual(logexpect.main(["add", "x", "(", "--log", str(self.log), "--state", str(state)]), 2)
        self.assertEqual(logexpect.main(["list", "--state", str(state)]), 0)


if __name__ == "__main__":
    unittest.main()
