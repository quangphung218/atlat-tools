#!/usr/bin/env python3
"""claude-skills — manage the global Claude Code skill folder with real usage numbers, not guesses. No LLM.

    claude-skills list [--json]              table: name · source · enabled · uses in N days · last edit · lines · lint
    claude-skills unused [--days 14]         enabled skills that no session invoked in the window
    claude-skills check [--refresh] [--json] skills installed with `npx skills` that drifted from upstream (git tree sha vs
                                             ~/.agents/.skill-lock.json, cached 24 h, needs `gh`) + lint of every skill
    claude-skills patch [--apply]            deterministic fixes: SKILL.md frontmatter missing name/description (backup first)
    claude-skills disable NAME... / enable NAME...   edit only `skillOverrides` in settings.json (backup + log)
    claude-skills upgrade NAME... | --drift  `npx skills update -g -y` after a backup; --drift = drifted, enabled and used
    claude-skills propose [--apply] [--disable-unused]   data-driven proposals; --apply does the deterministic part
                                             (patch, upgrade used+drifted; disabling unused needs --disable-unused too)

Sources: npx (symlink into ~/.agents/skills, tracked by ~/.agents/.skill-lock.json) · plugin copy (same name as a skill in
~/.claude/plugins/cache) · copy (plain folder) · synced (Claude app, never touched). Usage comes from `claude-observe` (run
automatically, or pass --usage FILE with its --json output). Backups: ~/.claude/backups/skills/<stamp>/. Log: ~/.claude/skills.log.
Lint: frontmatter name/description · SKILL.md over 300 lines · "You MUST"/HARD-GATE wording (fights agents that are allowed to
decide) · dead relative .md links.
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
GATE_RE = re.compile(r"You MUST|HARD-GATE|MUST use this", re.I)
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)\)|`((?:\./|\.\./)[\w./-]+\.md)`")
MESSAGES = {
    "en": {"head": "| Skill | Source | On | Uses {d}d | Edited | Lines | Lint |", "off": "off", "sum": "{n} skills, {on} enabled, {used} enabled and used, {lint} with lint findings.",
           "unused": "{n} enabled skills not invoked in {d} days: {list}", "drift": "**Upstream drift:** {n} npx skills{list}", "lint": "**Lint:** {n} skills", "plugins": "**Plugin cache:** {list}",
           "nopatch": "No frontmatter to patch.", "patch": "{name}: add frontmatter {how}", "patched": "(fixed)", "dry": "(add --apply to fix)", "nochange": "nothing to change",
           "set": "{verb} {n} skills: {list} (settings.json backed up to {dir})", "disabled": "disabled", "enabled": "enabled", "notnpx": "{name}: not an npx skill (not in the lock file), skipped",
           "upfail": "{name}: update failed or SKILL.md broken ({issues}); old copy kept in {dir}", "upok": "{name}: {a} → {b}", "samehash": " (hash unchanged, upstream may have moved the path)",
           "noprops": "No proposals.", "auto": " · auto", "nodrift": "No skill is both drifted and used.",
           "p_disable": "0 uses in {d} days", "p_upgrade": "drifted from upstream and used ({n} times)", "p_note_drift": "drifted from upstream but unused: not upgrading, consider disabling",
           "p_gone": "upstream removed this skill: keep the local copy or disable?", "p_patch": "{issues}", "p_soften": "contains 'You MUST'/HARD-GATE; reword if your agent is allowed to decide",
           "p_new": "you typed \"{ex}\" {n} times ({proj}): candidate for a skill or slash command",
           "lint_nomd": "no SKILL.md", "lint_nofm": "no frontmatter", "lint_name": "missing name", "lint_desc": "missing description", "lint_lines": "{n} lines", "lint_gate": "{n}× MUST/HARD-GATE", "lint_dead": "{n} dead links"},
    "vi": {"head": "| Skill | Nguồn | Bật | Gọi {d}d | Sửa | Dòng | Lint |", "off": "tắt", "sum": "{n} skill, {on} bật, {used} bật có dùng, {lint} có lint.",
           "unused": "{n} skill bật mà {d} ngày không gọi: {list}", "drift": "**Lệch upstream:** {n} skill npx{list}", "lint": "**Lint:** {n} skill", "plugins": "**Plugin trong cache:** {list}",
           "nopatch": "Không có frontmatter nào thiếu.", "patch": "{name}: thêm frontmatter {how}", "patched": "(đã sửa)", "dry": "(thêm --apply để sửa)", "nochange": "không có gì đổi",
           "set": "{verb} {n} skill: {list} (sao lưu settings.json ở {dir})", "disabled": "tắt", "enabled": "bật", "notnpx": "{name}: không phải skill npx (không có trong lock), bỏ qua",
           "upfail": "{name}: update lỗi hoặc SKILL.md hỏng ({issues}); giữ bản cũ ở {dir}", "upok": "{name}: {a} → {b}", "samehash": " (hash chưa đổi, upstream có thể đã đổi đường dẫn)",
           "noprops": "Không có đề xuất nào.", "auto": " · tự làm", "nodrift": "Không có skill nào vừa lệch upstream vừa đang dùng.",
           "p_disable": "0 lần gọi trong {d} ngày", "p_upgrade": "lệch upstream và có dùng ({n} lần)", "p_note_drift": "lệch upstream nhưng không dùng: không nâng cấp, cân nhắc tắt",
           "p_gone": "upstream đã bỏ skill này: giữ bản cũ hay tắt?", "p_patch": "{issues}", "p_soften": "có 'You MUST'/HARD-GATE; sửa lời nếu agent của anh được tự quyết",
           "p_new": "anh gõ \"{ex}\" {n} lần ({proj}): cân nhắc skill hay lệnh",
           "lint_nomd": "thiếu SKILL.md", "lint_nofm": "thiếu frontmatter", "lint_name": "thiếu name", "lint_desc": "thiếu description", "lint_lines": "{n} dòng", "lint_gate": "{n}× MUST/HARD-GATE", "lint_dead": "{n} link chết"},
}


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


class Skills:
    def __init__(self, claude_dir=None, lang="en", days=14, usage=None, observe_args=()):
        self.claude = Path(claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
        self.skills = self.claude / "skills"
        self.agents = HOME / ".agents" / "skills"
        self.lock_file = HOME / ".agents" / ".skill-lock.json"
        self.settings = self.claude / "settings.json"
        self.backups = self.claude / "backups" / "skills"
        self.cache = self.claude / "backups" / "skills-upstream.json"
        self.log_file = self.claude / "skills.log"
        self.m = MESSAGES.get(lang, MESSAGES["en"])
        self.days = days
        self._usage = usage
        self.observe_args = list(observe_args)

    def t(self, key, **kw):
        return self.m[key].format(**kw)

    # --- data --------------------------------------------------------------------------------------------------
    def lock(self):
        try:
            return json.loads(self.lock_file.read_text()).get("skills", {})
        except (OSError, ValueError):
            return {}

    def overrides(self):
        try:
            return json.loads(self.settings.read_text()).get("skillOverrides", {})
        except (OSError, ValueError):
            return {}

    def usage(self):
        """{skill: uses} and the observed window; runs claude-observe when no --usage file was given."""
        if self._usage is None:
            try:
                from atlat_tools import claude_observe  # installed as a package
                ob = claude_observe.Observer(self.claude, self.days, [a for a in self.observe_args])
                self._usage = ob.scan()
            except ImportError:
                here = Path(__file__).with_name("claude_observe.py")
                r = subprocess.run([sys.executable, str(here), "--json", "--days", str(self.days), "--claude-dir", str(self.claude)]
                                   + [x for p in self.observe_args for x in ("--exclude-prefix", p)], capture_output=True, text=True, timeout=600)
                self._usage = json.loads(r.stdout or "{}")
        return self._usage.get("skills", {}).get("used", {}), self._usage.get("days", self.days), self._usage.get("owner", {}).get("repeated", [])

    def plugin_names(self):
        base = self.claude / "plugins" / "cache"
        names = set()
        if base.is_dir():
            for sk in base.glob("*/*/*/skills/*/SKILL.md"):
                names.add(sk.parent.name)
        return names

    def lint(self, d):
        f = d / "SKILL.md"
        if not f.is_file():
            return [self.t("lint_nomd")]
        text = f.read_text(errors="ignore")
        out = []
        fm = frontmatter(text)
        if fm is None:
            out.append(self.t("lint_nofm"))
        else:
            if not fm.get("name"):
                out.append(self.t("lint_name"))
            if not fm.get("description"):
                out.append(self.t("lint_desc"))
        n = text.count("\n") + 1
        if n > 300:
            out.append(self.t("lint_lines", n=n))
        g = len(GATE_RE.findall(text))
        if g:
            out.append(self.t("lint_gate", n=g))
        dead = [p for p in (a or b for a, b in LINK_RE.findall(text)) if not (d / p).exists()]
        if dead:
            out.append(self.t("lint_dead", n=len(dead)))
        return out

    def inventory(self):
        lk, ov, plug = self.lock(), self.overrides(), self.plugin_names()
        used, days, _ = self.usage()
        rows = []
        if not self.skills.is_dir():
            return rows, days
        for d in sorted(self.skills.iterdir()):
            if not d.is_dir() or d.name == "synced":
                continue
            name = d.name
            if d.is_symlink():
                src = f"npx:{lk[name]['source']}" if name in lk else "npx"
            elif name in plug:
                src = "plugin-copy"
            else:
                src = "copy"
            f = d / "SKILL.md"
            rows.append({"name": name, "source": src, "enabled": ov.get(name) != "off", "used": used.get(name, 0),
                         "mtime": time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime)) if f.is_file() else "-",
                         "lines": (f.read_text(errors="ignore").count("\n") + 1) if f.is_file() else 0, "lint": self.lint(d)})
        return rows, days

    def upstream(self, refresh=False):
        if self.cache.exists() and not refresh:
            c = json.loads(self.cache.read_text())
            if time.time() - c.get("at", 0) < 86400:
                return c["trees"]
        trees = {}
        for repo in sorted({v["source"] for v in self.lock().values() if v.get("sourceType") == "github"}):
            try:
                r = subprocess.run(["gh", "api", f"repos/{repo}/git/trees/HEAD?recursive=1", "--jq", '.tree[] | select(.type=="tree") | "\\(.path) \\(.sha)"'],
                                   capture_output=True, text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                trees[repo] = dict(l.split(" ", 1) for l in r.stdout.splitlines() if " " in l)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({"at": time.time(), "trees": trees}))
        return trees

    def drift(self, refresh=False):
        """[(name, source, status)] with status 'drift' | 'gone' | 'unknown'."""
        trees, out = self.upstream(refresh), []
        for name, v in self.lock().items():
            if not (self.skills / name).exists():
                continue
            t = trees.get(v["source"])
            path = v["skillPath"].rsplit("/SKILL.md", 1)[0]
            if t is None:
                out.append((name, v["source"], "unknown"))
            elif path not in t:
                out.append((name, v["source"], "gone"))
            elif t[path] != v.get("skillFolderHash"):
                out.append((name, v["source"], "drift"))
        return out

    # --- actions -----------------------------------------------------------------------------------------------
    def backup(self, paths, tag):
        dest = self.backups / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
        dest.mkdir(parents=True, exist_ok=True)
        for p in paths:
            p = Path(p)
            if p.is_dir():
                shutil.copytree(p, dest / p.name, symlinks=False, dirs_exist_ok=True)
            elif p.is_file():
                shutil.copy2(p, dest / p.name)
        return dest

    def log(self, action, names, why):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {action} {' '.join(names)} — {why}\n")

    def set_overrides(self, names, value, why):
        names = [n for n in names if (self.skills / n).exists()]
        if not names:
            print(self.t("nochange")); return []
        s = json.loads(self.settings.read_text())
        ov = s.setdefault("skillOverrides", {})
        changed = []
        for n in names:
            if value == "off" and ov.get(n) != "off":
                ov[n] = "off"; changed.append(n)
            elif value == "on" and ov.get(n) == "off":
                del ov[n]; changed.append(n)
        if not changed:
            print(self.t("nochange")); return []
        dest = self.backup([self.settings], "settings")
        self.settings.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n")
        self.log("disable" if value == "off" else "enable", changed, why)
        print(self.t("set", verb=self.t("disabled" if value == "off" else "enabled"), n=len(changed), list=", ".join(changed), dir=dest))
        return changed

    def upgrade(self, names, why):
        lk, done = self.lock(), []
        for n in names:
            if n not in lk:
                print(self.t("notnpx", name=n)); continue
            dest = self.backup([self.agents / n], f"skill-{n}")
            before = lk[n].get("skillFolderHash")
            r = subprocess.run(["npx", "-y", "skills", "update", n, "-g", "-y"], cwd=HOME, capture_output=True, text=True, timeout=600)
            after = self.lock().get(n, {}).get("skillFolderHash")
            issues = self.lint(self.skills / n)
            if r.returncode != 0 or any(x in issues for x in (self.t("lint_nomd"), self.t("lint_nofm"), self.t("lint_name"), self.t("lint_desc"))):
                print(self.t("upfail", name=n, issues=issues, dir=dest)); continue
            done.append(n)
            print(self.t("upok", name=n, a=(before or "?")[:7], b=(after or "?")[:7]) + (self.t("samehash") if before == after else ""))
        if done:
            self.log("upgrade", done, why)
        return done

    def patch(self, apply):
        todo = []
        for d in sorted(self.skills.iterdir()) if self.skills.is_dir() else []:
            if not d.is_dir() or d.name == "synced" or d.is_symlink():
                continue
            f = d / "SKILL.md"
            if not f.is_file():
                continue
            text = f.read_text(errors="ignore")
            fm = frontmatter(text)
            if fm is None or not fm.get("name") or not fm.get("description"):
                todo.append((d.name, f, text, fm))
        if not todo:
            print(self.t("nopatch")); return []
        done = []
        for name, f, text, fm in todo:
            first = next((l.lstrip("# ").strip() for l in text.splitlines() if l.strip() and not l.startswith("---")), name)
            if fm is None:
                new = f"---\nname: {name}\ndescription: {first[:200]}\n---\n\n{text}"
            else:
                head, body = text.split("\n---\n", 1)
                add = (f"\nname: {name}" if not fm.get("name") else "") + (f"\ndescription: {first[:200]}" if not fm.get("description") else "")
                new = head + add + "\n---\n" + body
            print(f"{'✓' if apply else '·'} " + self.t("patch", name=name, how=self.t("patched") if apply else self.t("dry")))
            if apply:
                self.backup([f], f"skill-{name}")
                f.write_text(new)
                done.append(name)
        if done:
            self.log("patch", done, "frontmatter")
        return done

    def propose(self, rows, days, repeated):
        dr = {n: st for n, _, st in self.drift()}
        props = []
        for r in rows:
            n = r["name"]
            if r["enabled"] and r["used"] == 0:
                props.append({"kind": "disable", "name": n, "auto": False, "why": self.t("p_disable", d=days)})
            if n in dr and dr[n] == "drift" and r["enabled"]:
                if r["used"] > 0:
                    props.append({"kind": "upgrade", "name": n, "auto": True, "why": self.t("p_upgrade", n=r["used"])})
                else:
                    props.append({"kind": "note", "name": n, "auto": False, "why": self.t("p_note_drift")})
            if n in dr and dr[n] == "gone" and r["enabled"]:
                props.append({"kind": "note", "name": n, "auto": False, "why": self.t("p_gone")})
            if any(x in r["lint"] for x in (self.t("lint_nofm"), self.t("lint_name"), self.t("lint_desc"))) and not r["source"].startswith("npx"):
                props.append({"kind": "patch", "name": n, "auto": True, "why": self.t("p_patch", issues=", ".join(r["lint"]))})
            if any("MUST" in x for x in r["lint"]) and r["enabled"] and not r["source"].startswith("npx"):
                props.append({"kind": "soften", "name": n, "auto": False, "why": self.t("p_soften")})
        for rep in repeated:
            if rep["n"] >= 5 and not rep["example"].startswith("["):
                props.append({"kind": "new", "name": rep["key"], "auto": False, "why": self.t("p_new", ex=rep["example"], n=rep["n"], proj=rep["project"])})
        return props


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-skills", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("command", choices=["list", "unused", "check", "patch", "disable", "enable", "upgrade", "propose"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--drift", action="store_true", help="upgrade: every drifted, enabled and used skill")
    ap.add_argument("--disable-unused", action="store_true", help="propose --apply: also disable enabled skills with 0 uses")
    ap.add_argument("--refresh", action="store_true", help="check: re-fetch upstream trees")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--usage", help="JSON file from `claude-observe --json` (default: run it)")
    ap.add_argument("--exclude-prefix", action="append", default=[], help="passed to claude-observe")
    ap.add_argument("--claude-dir")
    ap.add_argument("--why", default="claude-skills", help="reason recorded in skills.log")
    ap.add_argument("--lang", choices=sorted(MESSAGES), default="en")
    a = ap.parse_args(argv)
    usage = json.loads(Path(a.usage).read_text()) if a.usage else None
    s = Skills(a.claude_dir, a.lang, a.days, usage, a.exclude_prefix)
    t = s.t
    if a.command == "list":
        rows, days = s.inventory()
        if a.json:
            print(json.dumps({"days": days, "skills": rows}, ensure_ascii=False, indent=1)); return 0
        print(t("head", d=days) + "\n|---|---|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['name']} | {r['source']} | {'✓' if r['enabled'] else t('off')} | {r['used']} | {r['mtime']} | {r['lines']} | {'; '.join(r['lint']) or '–'} |")
        on = [r for r in rows if r["enabled"]]
        print("\n" + t("sum", n=len(rows), on=len(on), used=sum(1 for r in on if r["used"]), lint=sum(1 for r in rows if r["lint"])))
        return 0
    if a.command == "unused":
        rows, days = s.inventory()
        un = [r["name"] for r in rows if r["enabled"] and r["used"] == 0]
        print(json.dumps(un) if a.json else t("unused", n=len(un), d=days, list=", ".join(un)))
        return 0
    if a.command == "check":
        dr = s.drift(a.refresh)
        rows, _ = s.inventory()
        linted = [(r["name"], r["lint"]) for r in rows if r["lint"]]
        base = s.claude / "plugins" / "cache"
        plugins = sorted({f"{p.parent.name}@{p.name}" for p in base.glob("*/*/*") if p.is_dir()}) if base.is_dir() else []
        if a.json:
            print(json.dumps({"drift": dr, "lint": linted, "plugins": plugins}, ensure_ascii=False, indent=1)); return 0
        print(t("drift", n=len(dr), list=(": " + ", ".join(f"{n} ({st})" if st != "drift" else n for n, _, st in dr)) if dr else ""))
        print(t("lint", n=len(linted)) + ("".join(f"\n- {n}: {'; '.join(l)}" for n, l in linted) if linted else ""))
        print(t("plugins", list=", ".join(plugins) or "-"))
        return 0
    if a.command == "patch":
        s.patch(a.apply); return 0
    if a.command in ("disable", "enable"):
        if not a.names:
            ap.error(f"{a.command} NAME...")
        s.set_overrides(a.names, "off" if a.command == "disable" else "on", a.why); return 0
    if a.command == "upgrade":
        names = a.names
        if a.drift:
            rows, _ = s.inventory()
            used = {r["name"] for r in rows if r["enabled"] and r["used"] > 0}
            names = [n for n, _, st in s.drift() if st == "drift" and n in used]
            if not names:
                print(t("nodrift")); return 0
        if not names:
            ap.error("upgrade NAME... | --drift")
        s.upgrade(names, a.why); return 0
    if a.command == "propose":
        rows, days = s.inventory()
        _, _, repeated = s.usage()
        props = s.propose(rows, days, repeated)
        if a.disable_unused:
            for p in props:
                if p["kind"] == "disable":
                    p["auto"] = True
        if a.json:
            print(json.dumps(props, ensure_ascii=False, indent=1)); return 0
        if not props:
            print(t("noprops")); return 0
        for p in props:
            print(f"- [{p['kind']}{t('auto') if p['auto'] else ''}] {p['name']}: {p['why']}")
        if a.apply:
            print()
            dis = [p["name"] for p in props if p["kind"] == "disable" and p["auto"]]
            up = [p["name"] for p in props if p["kind"] == "upgrade" and p["auto"]]
            if dis:
                s.set_overrides(dis, "off", t("p_disable", d=days))
            if up:
                s.upgrade(up, "drifted and used")
            if any(p["kind"] == "patch" for p in props):
                s.patch(True)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
