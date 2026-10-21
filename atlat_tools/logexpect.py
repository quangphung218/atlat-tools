#!/usr/bin/env python3
"""logexpect — "the feature I just shipped must actually run": register an expectation on a log file, get told when the first
real occurrence appears and whether it ended well. Tests pass, canaries pass, and the first real user still finds the bug;
this closes that gap. No LLM, no daemon unless you ask for `wait`.

    logexpect add "PR #62 rerun" 'event":"project_item_rerun' --log ~/app/app.log [--ok 'rerun_done'] [--bad 'rerun_failed']
    logexpect list                       pending / finished / stale expectations
    logexpect check                      scan the logs since each expectation was added; print new results (call from a hook or cron)
    logexpect wait [--timeout-min 720]   poll every minute until a new result, then print it (single instance per state file)
    logexpect done N                     drop one expectation after you have acted on it
    logexpect prune                      drop finished, stale and duplicate-named pending expectations

How a result is decided: the first line matching the main regex since `add` is the hit. Within the next 2 hours (`--follow-hours`),
lines that share the same correlation id (`"task":123`, `"project":9`, `"id":…`; set with --key) are checked against --bad then --ok.
No --ok/--bad means "seen" is the result. Pending for more than 7 days (`--stale-days`) is reported once as stale: either the regex is
wrong or nobody used the feature. Adding a name that is still pending replaces the old entry.
Logs: JSON lines with ts/time/timestamp, or text lines starting with an ISO timestamp; lines without a parseable time count as "now".
State: --state FILE (default ~/.local/share/logexpect/<log name>.json). Notify: --notify-cmd 'say {text}' runs on every new result
(macOS notification is used when available).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
ISO = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
KEYS = ("task", "project", "job", "run", "request_id", "id")
TEXT = {"en": {"ok": "✅ ok", "bad": "❌ failed", "seen": "👀 seen", "unclear": "❔ unclear (no ok/bad within the follow window)",
               "hit": "[{i}] {name}: happened, waiting for the outcome", "pending": "[{i}] {name}: no real run yet (since {since}, {days} days)",
               "stale": "[{i}] {name}: ⏸ stale after {days} days, no real run (wrong regex or unused feature; `done {i}` to drop)",
               "result": "[{i}] {name}: {mark} at {at} — {line}", "none": "No expectations.", "nonew": "No new results.",
               "added": "✓ waiting for the first real run: [{i}] {name}", "replaced": "✓ replaced the pending entry with the same name: [{i}] {name}",
               "dropped": "✓ dropped", "pruned": "✓ dropped {n} (finished, stale or duplicate), {left} still pending", "running": "another wait is running (pid {pid}), exiting",
               "nowait": "nothing pending", "timeout": "⏱ timed out without a new result", "badre": "bad regex: {e}"},
        "vi": {"ok": "✅ đạt", "bad": "❌ hỏng", "seen": "👀 đã xảy ra", "unclear": "❔ chưa rõ (không thấy ok/bad trong cửa sổ theo dõi)",
               "hit": "[{i}] {name}: đã chạy, đang chờ kết quả", "pending": "[{i}] {name}: chưa có lần chạy thật nào (từ {since}, {days} ngày)",
               "stale": "[{i}] {name}: ⏸ treo {days} ngày, chưa có lần chạy thật (regex sai hoặc tính năng chưa được dùng; `done {i}` để bỏ)",
               "result": "[{i}] {name}: {mark} lúc {at} — {line}", "none": "Không có kỳ vọng nào.", "nonew": "Chưa có kết quả mới.",
               "added": "✓ chờ lần chạy thật đầu tiên: [{i}] {name}", "replaced": "✓ thay mục cùng tên đang chờ: [{i}] {name}",
               "dropped": "✓ đã bỏ", "pruned": "✓ bỏ {n} mục (đã có kết quả, treo hoặc trùng tên), còn {left} đang chờ", "running": "đã có wait đang chạy (pid {pid}), thoát",
               "nowait": "không còn mục nào chờ", "timeout": "⏱ hết giờ, chưa có kết quả mới", "badre": "regex sai: {e}"}}


def parse_ts(line, default):
    """Time of a log line: JSON ts/time/timestamp, or a leading ISO stamp; else `default`."""
    if line.startswith("{"):
        try:
            d = json.loads(line)
            for k in ("ts", "time", "timestamp", "@timestamp"):
                v = d.get(k)
                if isinstance(v, str):
                    m = ISO.search(v)
                    if m:
                        return to_dt(m.group(1)), d
                if isinstance(v, (int, float)):
                    return datetime.fromtimestamp(v / (1000 if v > 1e11 else 1), timezone.utc), d
            return default, d
        except ValueError:
            pass
    m = ISO.match(line)
    return (to_dt(m.group(1)) if m else default), None


def to_dt(s):
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Expect:
    def __init__(self, state, lang="en", follow_hours=2.0, stale_days=7, notify_cmd=None, tail_bytes=16_000_000):
        self.state = Path(state)
        self.t = TEXT.get(lang, TEXT["en"])
        self.follow = timedelta(hours=follow_hours)
        self.stale = timedelta(days=stale_days)
        self.notify_cmd = notify_cmd
        self.tail_bytes = tail_bytes

    def load(self):
        return json.loads(self.state.read_text()) if self.state.exists() else []

    def save(self, items):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps(items, ensure_ascii=False, indent=2))

    def lines_since(self, log, t0):
        p = Path(log).expanduser()
        if not p.exists():
            return []
        with p.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - self.tail_bytes))
            raw = f.read().decode("utf-8", "replace").splitlines()
        now, out = datetime.now(timezone.utc), []
        for l in raw:
            t, d = parse_ts(l, now)
            if t is not None and t >= t0:
                out.append((t, d, l))
        return out

    def evaluate(self, e):
        """Update e in place; True when it just got a final result."""
        t0 = datetime.fromisoformat(e["since"])
        rows = self.lines_since(e["log"], t0)
        hit = next(((t, d, l) for t, d, l in rows if re.search(e["match"], l)), None)
        if not hit:
            return False
        t, d, l = hit
        e["hit"] = {"at": t.isoformat(), "line": l[:300]}
        keys = e.get("keys") or KEYS
        key = next((k for k in keys if isinstance(d, dict) and k in d), None)
        same = re.compile(rf'"{key}":\s*"?{re.escape(str(d[key]))}\b') if key else None
        window = [(tt, ll) for tt, _, ll in rows if t <= tt <= t + self.follow and (same is None or same.search(ll))]
        verdict = None
        for tt, ll in window:
            if e.get("bad") and re.search(e["bad"], ll):
                verdict = ("bad", tt, ll); break
            if e.get("ok") and re.search(e["ok"], ll):
                verdict = ("ok", tt, ll); break
        now = datetime.now(timezone.utc)
        if verdict:
            e["result"] = {"kind": verdict[0], "at": verdict[1].isoformat(), "line": verdict[2][:300]}
        elif not (e.get("ok") or e.get("bad")):
            e["result"] = {"kind": "seen", "at": t.isoformat(), "line": l[:300]}
        elif now > t + self.follow:
            e["result"] = {"kind": "unclear", "at": t.isoformat(), "line": l[:300]}
        return "result" in e

    def show(self, i, e):
        t = self.t
        if "result" in e:
            r = e["result"]
            at = datetime.fromisoformat(r["at"]).astimezone().strftime("%d/%m %H:%M")
            return t["result"].format(i=i, name=e["name"], mark=t[r["kind"]], at=at, line=r["line"][:180])
        if "hit" in e:
            return t["hit"].format(i=i, name=e["name"])
        t0 = datetime.fromisoformat(e["since"])
        days = (datetime.now(timezone.utc) - t0).days
        if e.get("stale"):
            return t["stale"].format(i=i, name=e["name"], days=days)
        return t["pending"].format(i=i, name=e["name"], since=t0.astimezone().strftime("%d/%m %H:%M"), days=days)

    def check(self):
        items, fresh = self.load(), []
        for i, e in enumerate(items):
            if "result" in e or e.get("dropped"):
                continue
            if self.evaluate(e):
                fresh.append(self.show(i, e))
            elif not e.get("stale") and datetime.now(timezone.utc) - datetime.fromisoformat(e["since"]) > self.stale:
                e["stale"] = True
                fresh.append(self.show(i, e))
        self.save(items)
        return fresh

    def notify(self, text):
        if self.notify_cmd:
            subprocess.run(self.notify_cmd.replace("{text}", text.replace("'", "’")), shell=True, capture_output=True)
        elif sys.platform == "darwin":
            safe = text.replace('"', "'").replace("\\", "")[:200]
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "logexpect"'], capture_output=True)

    def add(self, name, match, log, ok=None, bad=None, keys=None):
        for rx in (match, ok, bad):
            if rx:
                re.compile(rx)
        e = {"name": name, "match": match, "log": str(Path(log).expanduser()), "since": datetime.now(timezone.utc).isoformat()}
        if ok:
            e["ok"] = ok
        if bad:
            e["bad"] = bad
        if keys:
            e["keys"] = keys
        items = self.load()
        old = next((i for i, x in enumerate(items) if x["name"] == name and "result" not in x and not x.get("dropped")), None)
        if old is not None:
            items[old] = e
            self.save(items)
            return old, True
        items.append(e)
        self.save(items)
        return len(items) - 1, False

    def prune(self):
        items, seen, n = self.load(), set(), 0
        for e in items:
            if e.get("dropped"):
                continue
            dup = e["name"] in seen and "result" not in e
            seen.add(e["name"])
            if "result" in e or e.get("stale") or dup:
                e["dropped"] = True
                n += 1
        self.save(items)
        return n, sum(1 for e in items if not e.get("dropped"))

    def wait(self, timeout_min):
        pidf = self.state.with_suffix(".pid")
        try:
            other = int(pidf.read_text().split()[0])
            os.kill(other, 0)
            return 3, self.t["running"].format(pid=other)
        except (OSError, ValueError, IndexError):
            pass
        pidf.parent.mkdir(parents=True, exist_ok=True)
        pidf.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='minutes')}\n")
        try:
            limit = time.time() + 60 * timeout_min
            while time.time() < limit:
                fresh = self.check()
                if fresh:
                    for line in fresh:
                        self.notify(line)
                    return 0, "\n".join(fresh)
                if not any("result" not in e and not e.get("dropped") for e in self.load()):
                    return 0, self.t["nowait"]
                time.sleep(60)
            return 2, self.t["timeout"]
        finally:
            pidf.unlink(missing_ok=True)


def default_state(log):
    name = Path(log).expanduser().name or "log"
    h = hashlib.sha1(str(Path(log).expanduser()).encode()).hexdigest()[:8]
    return HOME / ".local" / "share" / "logexpect" / f"{name}-{h}.json"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="logexpect", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("command", choices=["add", "list", "check", "wait", "done", "prune"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--log", help="log file (required for add; for other commands it picks the default state file)")
    ap.add_argument("--state", help="state file (default ~/.local/share/logexpect/<log>-<hash>.json)")
    ap.add_argument("--ok"); ap.add_argument("--bad")
    ap.add_argument("--key", action="append", help="correlation field(s) to look for in JSON lines (default task, project, job, run, request_id, id)")
    ap.add_argument("--follow-hours", type=float, default=2.0)
    ap.add_argument("--stale-days", type=int, default=7)
    ap.add_argument("--timeout-min", type=float, default=720)
    ap.add_argument("--notify-cmd", help="shell command run for each new result, {text} is replaced")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    a = ap.parse_args(argv)
    if not a.state and not a.log:
        states = sorted((HOME / ".local" / "share" / "logexpect").glob("*.json"))
        if len(states) == 1:
            a.state = states[0]
        else:
            ap.error("--log or --state is required" + (f" (state files: {', '.join(s.name for s in states)})" if states else ""))
    state = a.state or default_state(a.log)
    x = Expect(state, a.lang, a.follow_hours, a.stale_days, a.notify_cmd)
    t = x.t
    if a.command == "add":
        if len(a.args) < 2 or not a.log:
            ap.error('add "NAME" REGEX --log FILE')
        try:
            i, replaced = x.add(a.args[0], a.args[1], a.log, a.ok, a.bad, a.key)
        except re.error as e:
            print(t["badre"].format(e=e)); return 2
        print((t["replaced"] if replaced else t["added"]).format(i=i, name=a.args[0])); return 0
    if a.command == "list":
        items = x.load()
        print("\n".join(x.show(i, e) for i, e in enumerate(items) if not e.get("dropped")) or t["none"]); return 0
    if a.command == "check":
        fresh = x.check()
        print("\n".join(fresh) or t["nonew"]); return 0
    if a.command == "done":
        items = x.load()
        items[int(a.args[0])]["dropped"] = True
        x.save(items); print(t["dropped"]); return 0
    if a.command == "prune":
        n, left = x.prune()
        print(t["pruned"].format(n=n, left=left)); return 0
    if a.command == "wait":
        rc, msg = x.wait(a.timeout_min)
        print(msg); return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
