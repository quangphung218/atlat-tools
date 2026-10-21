#!/usr/bin/env python3
"""agent-health — health of a machine that runs coding agents: who is eating the CPU and the RAM, which agent sessions are idle,
which dev servers and headless browsers were left behind, whether the API is reachable, what fills the disk. Read-only unless you
pass --fix, and --fix only kills what is provably orphaned. macOS first (memory_pressure, lsof); Linux best effort (/proc, ss).

    agent-health                 full markdown report
    agent-health --brief         one line for a status bar or a daily note
    agent-health --json
    agent-health --disk          add cache and node_modules sizes (slower: du)
    agent-health --idle-hours 12 flag agent sessions idle longer than this (tty inactivity)
    agent-health --api URL       reachability probe (default https://api.anthropic.com/; any HTTP status counts as reachable)
    agent-health --fix           kill dev servers / headless browsers whose parent is gone and that have run for > 2 h
    agent-health --lang vi

Sessions = processes named claude, codex, gemini, cursor-agent, aider (configurable with --agent NAME). Idle = time since the
session's tty was last touched. Stray = node/python/vitest/chrome processes re-parented to init (their launcher died).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
NOW = time.time()
AGENTS = ("claude", "codex", "gemini", "cursor-agent", "aider", "opencode", "goose")
STRAY_RE = re.compile(r"\b(node|python3?|vitest|jest|esbuild|vite|playwright|chrom(e|ium)(-headless-shell)?|tsx|bun|deno|next-server)\b", re.I)
APP_RE = re.compile(r"/Applications/|crashpad|Electron Framework|\.app/Contents/|/System/|/usr/libexec/")
DEV_CMDS = {"node", "python", "python3", "bun", "deno", "ruby", "php", "tsx", "vite", "next-server", "uvicorn", "gunicorn", "flask"}
TEXT = {"en": {"title": "# Agent host health · {at}", "verdict_ok": "**Verdict:** nothing to worry about.", "verdict": "**Verdict:** {n} things to look at.",
               "load": "load {l1:.1f}/{l5:.1f}/{l15:.1f} ({cpu} cores) · RAM free {ram}% · swap {swap:.1f} GB · API {api} · disk {disk}% used, {free:.0f} GB free",
               "w_load": "load {l:.1f} > {cpu} cores{top}", "w_ram": "RAM free {ram}%", "w_swap": "swap {swap:.1f} GB", "w_disk": "{free:.0f} GB free on disk", "w_api": "API unreachable ({url}): sessions will hang waiting for responses",
               "top": " ({name} pid {pid} at {cpu:.0f}%{owner})", "owner_session": ", child of {agent} pid {pid}",
               "sessions": "## Agent sessions: {n}, {rss} total", "s_head": "| pid | agent | cwd | age | RSS | idle |", "w_idle": "{agent} {pid} in {cwd} idle {idle}, {rss}",
               "stray": "## Stray processes", "s_none": "- none", "s_row": "- pid {pid} ({age}, {rss}, parent gone): `{cmd}`", "l_row": "- {cmd} pid {pid} listening on {name}{age}",
               "w_stray": "{n} processes re-parented to init", "w_listen": "{n} dev servers still listening", "fixed": "- killed pid {pid} ({cmd})",
               "disk": "## Disk", "d_cache": "- Large caches (~/Library/Caches, {total} total): {items}", "d_nm": "- node_modules in repos with no commit for 60+ days ({total}): {items}", "d_tr": "- Claude Code transcripts (~/.claude/projects): {size}",
               "brief": "{load} · {n} sessions{warn}", "brief_warn": " · ⚠️ {n}: {items}", "h": "{h:.0f} h", "m": "{m:.0f} min", "d": "{d:.0f} d"},
        "vi": {"title": "# Sức khoẻ máy chạy agent · {at}", "verdict_ok": "**Kết luận:** không có gì đáng lo.", "verdict": "**Kết luận:** {n} điều cần để ý.",
               "load": "load {l1:.1f}/{l5:.1f}/{l15:.1f} ({cpu} nhân) · RAM trống {ram}% · swap {swap:.1f} GB · API {api} · đĩa {disk}% dùng, còn {free:.0f} GB",
               "w_load": "load {l:.1f} > {cpu} nhân{top}", "w_ram": "RAM trống {ram}%", "w_swap": "swap {swap:.1f} GB", "w_disk": "đĩa còn {free:.0f} GB", "w_api": "không tới được API ({url}): phiên agent sẽ treo chờ",
               "top": " ({name} pid {pid} chiếm {cpu:.0f}%{owner})", "owner_session": ", con của {agent} pid {pid}",
               "sessions": "## Phiên agent: {n}, tổng {rss}", "s_head": "| pid | agent | cwd | tuổi | RSS | idle |", "w_idle": "{agent} {pid} ở {cwd} idle {idle}, {rss}",
               "stray": "## Tiến trình lạc", "s_none": "- không có", "s_row": "- pid {pid} ({age}, {rss}, cha đã chết): `{cmd}`", "l_row": "- {cmd} pid {pid} nghe {name}{age}",
               "w_stray": "{n} tiến trình đã đổi cha sang init", "w_listen": "{n} dev server còn nghe cổng", "fixed": "- đã tắt pid {pid} ({cmd})",
               "disk": "## Đĩa", "d_cache": "- Cache lớn (~/Library/Caches, tổng {total}): {items}", "d_nm": "- node_modules của repo > 60 ngày không commit ({total}): {items}", "d_tr": "- Transcript Claude Code (~/.claude/projects): {size}",
               "brief": "{load} · {n} phiên{warn}", "brief_warn": " · ⚠️ {n}: {items}", "h": "{h:.0f} giờ", "m": "{m:.0f} phút", "d": "{d:.0f} ngày"}}


def run(args, timeout=25):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def etime_seconds(e):
    d, _, rest = e.rpartition("-")
    parts = [int(x) for x in rest.split(":") if x.isdigit()] or [0]
    s = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    return s + (int(d) * 86400 if d else 0)


def ps_all():
    out = run(["ps", "-axo", "pid=,ppid=,tty=,etime=,rss=,%cpu=,command="], timeout=10)
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, tty, etime, rss, cpu, cmd = parts
        try:
            procs.append({"pid": int(pid), "ppid": int(ppid), "tty": tty, "etime": etime, "rss": int(rss), "cpu": float(cpu), "cmd": cmd})
        except ValueError:
            continue
    return procs


def cwds(pids):
    if not pids or not shutil.which("lsof"):
        return {p: (os.readlink(f"/proc/{p}/cwd") if Path(f"/proc/{p}/cwd").exists() else "?") for p in pids} if Path("/proc").exists() else {}
    out = run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(map(str, pids))], timeout=20)
    res, cur = {}, None
    for line in out.splitlines():
        if line.startswith("p"):
            cur = int(line[1:])
        elif line.startswith("n") and cur:
            res[cur] = line[1:]
    return res


def listeners():
    """[(cmd, pid, address)] of TCP listeners."""
    out = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=15) if shutil.which("lsof") else ""
    rows, seen = [], set()
    if out:
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 9 and (int(parts[1]), parts[8]) not in seen:
                seen.add((int(parts[1]), parts[8]))
                rows.append((parts[0], int(parts[1]), parts[8]))
        return rows
    out = run(["ss", "-ltnp"], timeout=10)
    for line in out.splitlines()[1:]:
        m = re.search(r'\s(\S+)\s+users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            rows.append((m.group(2), int(m.group(3)), m.group(1)))
    return rows


class Health:
    def __init__(self, lang="en", idle_hours=12.0, api="https://api.anthropic.com/", agents=AGENTS, disk=False, fix=False):
        self.t = TEXT.get(lang, TEXT["en"])
        self.idle_hours, self.api, self.agents, self.disk, self.do_fix = idle_hours, api, tuple(agents), disk, fix
        self.procs = ps_all()
        self.by_pid = {p["pid"]: p for p in self.procs}
        self.warns, self.fixed = [], []

    def age(self, seconds):
        h = seconds / 3600
        return self.t["m"].format(m=h * 60) if h < 1 else (self.t["h"].format(h=h) if h < 48 else self.t["d"].format(d=h / 24))

    @staticmethod
    def mb(kb):
        return f"{kb / 1024:.0f} MB"

    @staticmethod
    def gb(kb):
        return f"{kb / 1048576:.1f} GB"

    def is_agent(self, p):
        name = p["cmd"].split()[0].rsplit("/", 1)[-1] if p["cmd"] else ""
        return name in self.agents

    def owner(self, pid, hops=12):
        q = self.by_pid.get(pid)
        while q and q["ppid"] > 1 and hops:
            q = self.by_pid.get(q["ppid"]); hops -= 1
            if q and self.is_agent(q):
                return self.t["owner_session"].format(agent=q["cmd"].split()[0].rsplit("/", 1)[-1], pid=q["pid"])
        return ""

    # 1. load
    def load(self):
        l1, l5, l15 = os.getloadavg()
        cpu = os.cpu_count() or 1
        ram = None
        mp = run(["memory_pressure"], timeout=10)
        m = re.search(r"free percentage: (\d+)%", mp)
        if m:
            ram = int(m.group(1))
        elif Path("/proc/meminfo").exists():
            info = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
            if "MemTotal" in info and "MemAvailable" in info:
                ram = int(info["MemAvailable"]) * 100 // int(info["MemTotal"])
        swap = 0.0
        sw = run(["sysctl", "-n", "vm.swapusage"], timeout=5)
        m = re.search(r"used = ([\d.]+)M", sw)
        if m:
            swap = float(m.group(1)) / 1024
        elif Path("/proc/meminfo").exists():
            info = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
            swap = (int(info.get("SwapTotal", 0)) - int(info.get("SwapFree", 0))) / 1048576
        du = shutil.disk_usage(str(HOME))
        free_gb, disk_pct = du.free / 1e9, du.used * 100 / du.total
        api_ok = True
        if self.api:
            code = run(["curl", "-s", "-o", os.devnull, "-w", "%{http_code}", "--max-time", "4", self.api], timeout=8).strip()
            api_ok = code.isdigit() and code != "000"
        top = ""
        if l1 > cpu:
            hot = max(self.procs, key=lambda p: p["cpu"], default=None)
            if hot and hot["cpu"] >= 100:
                top = self.t["top"].format(name=hot["cmd"].split()[0].rsplit("/", 1)[-1], pid=hot["pid"], cpu=hot["cpu"], owner=self.owner(hot["pid"]))
            self.warns.append(self.t["w_load"].format(l=l1, cpu=cpu, top=top))
        if ram is not None and ram < 20:
            self.warns.append(self.t["w_ram"].format(ram=ram))
        if swap > 4:
            self.warns.append(self.t["w_swap"].format(swap=swap))
        if free_gb < 30:
            self.warns.append(self.t["w_disk"].format(free=free_gb))
        if not api_ok:
            self.warns.append(self.t["w_api"].format(url=self.api))
        self.load_line = self.t["load"].format(l1=l1, l5=l5, l15=l15, cpu=cpu, ram=ram if ram is not None else "?", swap=swap, api="✓" if api_ok else "✗", disk=f"{disk_pct:.0f}", free=free_gb)
        return {"load": [l1, l5, l15], "cpus": cpu, "ram_free_pct": ram, "swap_gb": round(swap, 1), "disk_free_gb": round(free_gb), "disk_used_pct": round(disk_pct), "api_ok": api_ok}

    # 2. sessions
    def sessions(self):
        rows, cl = [], [p for p in self.procs if self.is_agent(p)]
        cw = cwds([p["pid"] for p in cl])
        for p in sorted(cl, key=lambda p: -p["rss"]):
            cwd = cw.get(p["pid"], "?")
            idle = None
            if p["tty"] not in ("??", "-", "?"):
                try:
                    idle = NOW - os.stat(f"/dev/{p['tty']}").st_atime
                except OSError:
                    pass
            agent = p["cmd"].split()[0].rsplit("/", 1)[-1]
            row = {"pid": p["pid"], "agent": agent, "cwd": cwd.replace(str(HOME), "~"), "age_s": etime_seconds(p["etime"]), "rss_kb": p["rss"], "idle_s": idle}
            if idle is not None and idle / 3600 > self.idle_hours:
                row["idle_flag"] = True
                self.warns.append(self.t["w_idle"].format(agent=agent, pid=p["pid"], cwd=Path(cwd).name, idle=self.age(idle), rss=self.mb(p["rss"])))
            rows.append(row)
        return rows

    # 3. stray + listeners
    def stray(self):
        stray = [p for p in self.procs if p["ppid"] == 1 and not APP_RE.search(p["cmd"]) and STRAY_RE.search(p["cmd"].split()[0].rsplit("/", 1)[-1] if p["cmd"] else "")]
        listen = []
        for cmd, pid, name in listeners():
            if cmd.lower() not in DEV_CMDS:
                continue
            pr = self.by_pid.get(pid)
            orphan = pr is None or pr["ppid"] <= 1 or pr["ppid"] not in self.by_pid
            old = pr is not None and etime_seconds(pr["etime"]) > 7200
            if orphan or old:
                listen.append({"cmd": cmd, "pid": pid, "addr": name, "age_s": etime_seconds(pr["etime"]) if pr else None, "orphan": orphan})
        if stray:
            self.warns.append(self.t["w_stray"].format(n=len(stray)))
        if listen:
            self.warns.append(self.t["w_listen"].format(n=len(listen)))
        if self.do_fix:
            for p in stray + [self.by_pid[l["pid"]] for l in listen if l["orphan"] and l["pid"] in self.by_pid and etime_seconds(self.by_pid[l["pid"]]["etime"]) > 7200]:
                try:
                    os.kill(p["pid"], 15)
                    self.fixed.append(self.t["fixed"].format(pid=p["pid"], cmd=p["cmd"][:60]))
                except OSError:
                    pass
        return [{"pid": p["pid"], "age_s": etime_seconds(p["etime"]), "rss_kb": p["rss"], "cmd": p["cmd"][:120]} for p in stray], listen

    # 4. disk
    def disk_report(self):
        rows = []
        du_kb = lambda p: int(run(["du", "-sk", str(p)], timeout=120).split()[0] or 0) if run(["du", "-sk", str(p)], timeout=120) else 0
        caches = HOME / "Library/Caches"
        if caches.is_dir():
            sizes = sorted(((du_kb(d), d.name) for d in caches.iterdir() if d.is_dir()), reverse=True)
            top = [f"{n} {self.gb(k)}" for k, n in sizes[:4] if k > 512 * 1024]
            if top:
                rows.append(self.t["d_cache"].format(total=self.gb(sum(k for k, _ in sizes)), items=", ".join(top)))
        stale = []
        for base in (HOME / "Documents", HOME / "code", HOME / "src", HOME / "projects", HOME / "repos"):
            if not base.is_dir():
                continue
            for git_dir in base.glob("*/.git"):
                repo = git_dir.parent
                nm = repo / "node_modules"
                if not nm.is_dir():
                    continue
                last = run(["git", "-C", str(repo), "log", "-1", "--format=%ct"], timeout=10).strip()
                if last.isdigit() and NOW - int(last) > 60 * 86400:
                    stale.append((du_kb(nm), repo.name, (NOW - int(last)) / 86400))
        stale.sort(reverse=True)
        if stale:
            rows.append(self.t["d_nm"].format(total=self.gb(sum(k for k, _, _ in stale)), items=", ".join(f"{n} {self.gb(k)} ({d:.0f}d)" for k, n, d in stale[:5])))
        proj = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "projects"
        if proj.is_dir():
            rows.append(self.t["d_tr"].format(size=self.gb(du_kb(proj))))
        return rows

    def report(self):
        load = self.load()
        sessions = self.sessions()
        stray, listen = self.stray()
        disk = self.disk_report() if self.disk else []
        data = {"at": time.strftime("%Y-%m-%d %H:%M"), **load, "sessions": sessions, "stray": stray, "listeners": listen, "disk": disk, "warnings": self.warns, "fixed": self.fixed}
        t = self.t
        lines = [t["title"].format(at=data["at"]), "", t["verdict"].format(n=len(self.warns)) if self.warns else t["verdict_ok"]]
        lines += [f"- {w}" for w in self.warns] + ["", f"- {self.load_line}", "", t["sessions"].format(n=len(sessions), rss=self.gb(sum(s["rss_kb"] for s in sessions)))]
        if sessions:
            lines += [t["s_head"], "|---|---|---|---|---|---|"]
            lines += [f"| {s['pid']} | {s['agent']} | {Path(s['cwd']).name or s['cwd']} | {self.age(s['age_s'])} | {self.mb(s['rss_kb'])} | {self.age(s['idle_s']) if s['idle_s'] is not None else '?'}{' ⚠️' if s.get('idle_flag') else ''} |" for s in sessions]
        lines += ["", t["stray"]]
        lines += [t["s_row"].format(pid=s["pid"], age=self.age(s["age_s"]), rss=self.mb(s["rss_kb"]), cmd=s["cmd"][:90]) for s in stray]
        lines += [t["l_row"].format(cmd=l["cmd"], pid=l["pid"], name=l["addr"], age=f" ({self.age(l['age_s'])})" if l["age_s"] else "") for l in listen]
        if not stray and not listen:
            lines.append(t["s_none"])
        if self.fixed:
            lines += [""] + self.fixed
        if disk:
            lines += ["", t["disk"]] + disk
        return "\n".join(lines) + "\n", data

    def brief(self):
        load = self.load(); sessions = self.sessions(); self.stray()
        warn = self.t["brief_warn"].format(n=len(self.warns), items="; ".join(self.warns[:3])) if self.warns else ""
        return self.t["brief"].format(load=self.load_line, n=len(sessions), warn=warn)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-health", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--disk", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--idle-hours", type=float, default=12.0)
    ap.add_argument("--api", default="https://api.anthropic.com/", help="URL to probe; empty string disables")
    ap.add_argument("--agent", action="append", help="extra process name to treat as an agent session (repeatable)")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    a = ap.parse_args(argv)
    h = Health(a.lang, a.idle_hours, a.api, AGENTS + tuple(a.agent or ()), a.disk, a.fix)
    if a.brief:
        print(h.brief()); return 0
    md, data = h.report()
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(md, end="")
    return 1 if h.warns else 0


if __name__ == "__main__":
    sys.exit(main())
