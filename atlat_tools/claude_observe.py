#!/usr/bin/env python3
"""claude-observe — what you actually do with Claude Code: prompts per project, repeated requests, which skills and tools
get used, where tools fail. Reads only local files (~/.claude/history.jsonl and ~/.claude/projects/*/*.jsonl). No LLM, no network.

    claude-observe                  last 14 days, markdown summary
    claude-observe --days 30        another window
    claude-observe --json           machine-readable
    claude-observe --out DIR        also write DIR/<date>.md and DIR/<date>.json
    claude-observe --exclude-prefix "Em là vai"   drop prompts starting with a prefix (e.g. ones another agent typed for you)
    claude-observe --exclude-project SUBSTR       drop sessions whose cwd contains SUBSTR (bots, worktrees of other agents)
    claude-observe --lang vi        Vietnamese output

Privacy: prompts are kept to their first 80 characters, strings that look like keys or tokens are masked, and nothing leaves the
machine. ~800 MB of transcripts scan in a few seconds because lines are filtered by substring before JSON parsing.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|xox[a-z]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}|[A-Fa-f0-9]{32,})")
MESSAGES = {
    "en": {"title": "# Claude Code · last {days} days · {at} (since {since})", "owner": "**You:** {n} prompts in {p} projects ({top}) · by time of day: {buckets} · slash commands: {cmds}",
           "repeated": "**Repeated requests (≥ 3 times, first 5 words):**", "skills": "**Skills:** {used}/{enabled} enabled skills were invoked ({list}) · {unused} enabled but unused in {days} days · {off} disabled (skillOverrides).",
           "sessions": "**Sessions:** {n} ({mb} MB of transcripts): {by}", "tools": "- Tools: {tools}", "agent": "- `Agent` tool (subagents): {by}", "errors": "- Tool errors (tool_result is_error): {by}",
           "none": "none", "morning": "morning", "afternoon": "afternoon", "evening": "evening", "night": "night",
           "brief": "{days} days: {n} prompts in {p} projects · {used}/{enabled} enabled skills used, {unused} unused · {rep} repeated requests"},
    "vi": {"title": "# Claude Code · {days} ngày · {at} (từ {since})", "owner": "**Anh:** {n} prompt ở {p} project ({top}) · theo buổi: {buckets} · lệnh: {cmds}",
           "repeated": "**Việc lặp (≥ 3 lần, khoá 5 từ đầu):**", "skills": "**Skill:** {used}/{enabled} skill đang bật được gọi ({list}) · {unused} bật mà {days} ngày không dùng · {off} đã tắt (skillOverrides).",
           "sessions": "**Phiên:** {n} ({mb} MB transcript): {by}", "tools": "- Tool: {tools}", "agent": "- Tool `Agent` (subagent): {by}", "errors": "- Tool lỗi (tool_result is_error): {by}",
           "none": "không có", "morning": "sáng", "afternoon": "chiều", "evening": "tối", "night": "đêm",
           "brief": "{days} ngày: {n} prompt ở {p} project · {used}/{enabled} skill bật có dùng, {unused} không dùng · {rep} việc lặp"},
}


def redact(s):
    return SECRET.sub("<key>", s)


def norm_key(text):
    """Key for grouping repeated requests: first 5 words, lowercase, punctuation removed; slash commands and short texts are skipped."""
    if text.startswith("/"):
        return ""
    words = re.sub(r"[^\w\s]", " ", text.lower()).split()
    return " ".join(words[:5]) if len(words) >= 3 else ""


class Observer:
    def __init__(self, claude_dir=None, days=14, exclude_prefix=(), exclude_project=(), lang="en", tz=None):
        self.claude = Path(claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
        self.days = days
        self.since_s = time.time() - days * 86400
        self.exclude_prefix = tuple(exclude_prefix) + ("[Pasted text", "[Image")
        self.exclude_project = tuple(exclude_project)
        self.m = MESSAGES.get(lang, MESSAGES["en"])
        self.tz = tz or datetime.datetime.now().astimezone().tzinfo

    def label(self, path):
        p = str(path or "")
        if any(x in p for x in self.exclude_project):
            return None
        m = re.search(r"/orca/workspaces/([^/]+)/", p)  # Orca worktrees: show the repo name
        if m:
            return m.group(1)
        if "/scratchpad/" in p or p.startswith("/private/tmp/claude-"):
            return "(scratchpad)"
        if p.startswith(("/private/var/folders", "/var/folders", "/tmp/")):
            return "(tmp)"
        return p.replace(str(HOME), "~") or "?"

    def prompts(self):
        rows = []
        f = self.claude / "history.jsonl"
        if not f.exists():
            return rows
        since_ms = self.since_s * 1000
        for line in f.open(errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = int(d.get("timestamp") or 0)
            if ts < since_ms:
                continue
            text = (d.get("display") or "").strip()
            if not text or text.startswith(self.exclude_prefix):
                continue
            proj = self.label(d.get("project"))
            if proj is None:
                continue
            t = datetime.datetime.fromtimestamp(ts / 1000, self.tz)
            rows.append({"day": t.strftime("%Y-%m-%d"), "hour": t.hour, "project": proj, "text": redact(text[:80]),
                         "command": text.startswith("/"), "key": norm_key(text)})
        return rows

    def transcripts(self):
        tools, skills, agent_by, errors_by, sessions_by = (collections.Counter() for _ in range(5))
        skills_by = collections.defaultdict(collections.Counter)
        nfiles = nbytes = 0
        root = self.claude / "projects"
        if not root.is_dir():
            return {"files": 0, "mb": 0, "tools": {}, "skills": {}, "skills_by_project": {}, "agent_by_project": {}, "tool_errors_by_project": {}, "sessions_by_project": {}}
        for d in root.iterdir():
            # project folders are the cwd with "/" replaced by "-", so match the exclusion in both spellings
            if not d.is_dir() or any(x in d.name or x.replace("/", "-") in d.name for x in self.exclude_project):
                continue
            for f in d.glob("*.jsonl"):
                try:
                    if f.stat().st_mtime < self.since_s:
                        continue
                except OSError:
                    continue
                proj, excluded = None, False
                with f.open("rb") as fh:
                    for raw in fh:
                        nbytes += len(raw)
                        if proj is None and b'"cwd"' in raw:
                            try:
                                proj = self.label(json.loads(raw).get("cwd"))
                            except ValueError:
                                pass
                            if proj is None:
                                excluded = True
                                break
                        if b'"tool_use"' not in raw and b'"is_error":true' not in raw:
                            continue
                        try:
                            msg = json.loads(raw).get("message") or {}
                        except ValueError:
                            continue
                        c = msg.get("content")
                        if not isinstance(c, list):
                            continue
                        for b in c:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "tool_use":
                                name = b.get("name") or "?"
                                tools[name] += 1
                                if name == "Skill":
                                    sk = (b.get("input") or {}).get("skill") or "?"
                                    skills[sk] += 1
                                    skills_by[sk][proj or d.name] += 1
                                elif name == "Agent":
                                    agent_by[proj or d.name] += 1
                            elif b.get("type") == "tool_result" and b.get("is_error"):
                                errors_by[proj or d.name] += 1
                if excluded:
                    continue
                nfiles += 1
                sessions_by[proj or d.name] += 1
        return {"files": nfiles, "mb": round(nbytes / 1e6), "tools": dict(tools.most_common()), "skills": dict(skills.most_common()),
                "skills_by_project": {k: dict(v) for k, v in skills_by.items()}, "agent_by_project": dict(agent_by.most_common()),
                "tool_errors_by_project": dict(errors_by.most_common()), "sessions_by_project": dict(sessions_by.most_common())}

    def skills_state(self):
        """Enabled global skills = folders in <claude>/skills (except synced) not switched off in settings.json skillOverrides."""
        try:
            off = {k for k, v in json.load((self.claude / "settings.json").open()).get("skillOverrides", {}).items() if v == "off"}
        except (OSError, ValueError):
            off = set()
        sdir = self.claude / "skills"
        names = [d.name for d in sdir.iterdir() if d.is_dir() and d.name != "synced"] if sdir.is_dir() else []
        return sorted(n for n in names if n not in off), sorted(off)

    def scan(self):
        prompts = self.prompts()
        per_day = collections.Counter(p["day"] for p in prompts)
        per_proj = collections.Counter(p["project"] for p in prompts)
        m = self.m
        bucket = lambda h: m["morning"] if 5 <= h < 12 else m["afternoon"] if 12 <= h < 18 else m["evening"] if 18 <= h < 24 else m["night"]
        buckets = collections.Counter(bucket(p["hour"]) for p in prompts)
        keys = collections.Counter(p["key"] for p in prompts if p["key"])
        repeated = []
        for k, n in keys.most_common():
            if n < 3:
                break
            ex = next(p for p in prompts if p["key"] == k)
            repeated.append({"n": n, "key": k, "example": ex["text"], "project": ex["project"]})
        commands = collections.Counter(p["text"].split()[0] for p in prompts if p["command"])
        tr = self.transcripts()
        enabled, off = self.skills_state()
        used = tr["skills"]
        return {"days": self.days, "since": datetime.datetime.fromtimestamp(self.since_s, self.tz).strftime("%Y-%m-%d"),
                "at": datetime.datetime.now(self.tz).strftime("%Y-%m-%d %H:%M"),
                "owner": {"total": len(prompts), "per_day": dict(sorted(per_day.items())), "per_project": dict(per_proj.most_common()),
                          "buckets": dict(buckets), "repeated": repeated[:15], "commands": dict(commands.most_common(10))},
                "sessions": tr,
                "skills": {"enabled": enabled, "off": off, "used": used, "used_enabled": [s for s in enabled if s in used], "unused": [s for s in enabled if s not in used]}}

    def markdown(self, o):
        m, ow, tr, sk = self.m, o["owner"], o["sessions"], o["skills"]
        join = lambda d, n=6: ", ".join(f"{k} {v}" for k, v in list(d.items())[:n]) or m["none"]
        lines = [m["title"].format(days=o["days"], at=o["at"], since=o["since"]), "",
                 m["owner"].format(n=ow["total"], p=len(ow["per_project"]), top=" · ".join(f"{k} {v}" for k, v in list(ow["per_project"].items())[:5]) or m["none"],
                                   buckets=" ".join(f"{k} {v}" for k, v in ow["buckets"].items()) or m["none"], cmds=join(ow["commands"], 10)), ""]
        if ow["repeated"]:
            lines += [m["repeated"]] + [f"- {r['n']}× \"{r['example']}\" ({r['project']})" for r in ow["repeated"]] + [""]
        lines += [m["skills"].format(used=len(sk["used_enabled"]), enabled=len(sk["enabled"]), list=join(sk["used"], 10), unused=len(sk["unused"]), days=o["days"], off=len(sk["off"])), ""]
        lines += [m["sessions"].format(n=tr["files"], mb=tr["mb"], by=join(tr["sessions_by_project"])), m["tools"].format(tools=" · ".join(f"{k} {v}" for k, v in list(tr["tools"].items())[:8]) or m["none"]),
                  m["agent"].format(by=join(tr["agent_by_project"], 5)), m["errors"].format(by=join(tr["tool_errors_by_project"], 5)), ""]
        return "\n".join(lines)

    def brief(self, o):
        ow, sk = o["owner"], o["skills"]
        return self.m["brief"].format(days=o["days"], n=ow["total"], p=len(ow["per_project"]), used=len(sk["used_enabled"]), enabled=len(sk["enabled"]), unused=len(sk["unused"]), rep=len(ow["repeated"]))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-observe", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--brief", action="store_true", help="one line")
    ap.add_argument("--out", help="directory to write <date>.md and <date>.json into")
    ap.add_argument("--claude-dir", help="Claude config dir (default $CLAUDE_CONFIG_DIR or ~/.claude)")
    ap.add_argument("--exclude-prefix", action="append", default=[], help="drop prompts that start with this text (repeatable)")
    ap.add_argument("--exclude-project", action="append", default=[], help="drop sessions whose cwd contains this text (repeatable)")
    ap.add_argument("--lang", choices=sorted(MESSAGES), default="en")
    a = ap.parse_args(argv)
    ob = Observer(a.claude_dir, a.days, a.exclude_prefix, a.exclude_project, a.lang)
    o = ob.scan()
    if a.out:
        out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
        day = o["at"][:10]
        (out / f"{day}.json").write_text(json.dumps(o, ensure_ascii=False))
        (out / f"{day}.md").write_text(ob.markdown(o) + "\n")
    if a.json:
        print(json.dumps(o, ensure_ascii=False, indent=1))
    elif a.brief:
        print(ob.brief(o))
    else:
        print(ob.markdown(o))
    return 0


if __name__ == "__main__":
    sys.exit(main())
