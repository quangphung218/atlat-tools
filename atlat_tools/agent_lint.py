#!/usr/bin/env python3
"""agent-lint — deterministic linter for coding-agent configuration (Claude Code, Codex, Cursor). No LLM, no network.

    agent-lint                      lint the current directory
    agent-lint PATH [PATH ...]      lint these project roots
    agent-lint --global             also lint ~/.claude (or $CLAUDE_CONFIG_DIR): settings, skills, CLAUDE.md
    agent-lint --json               machine-readable output
    agent-lint --out report.md      write the markdown report to a file
    agent-lint --lang vi            Vietnamese messages
    agent-lint --max-lines 300      length threshold for CLAUDE.md / AGENTS.md

What it checks (every finding is a fact you can verify, no judgement calls):
  - CLAUDE.md / AGENTS.md at the root and up to two folders deep: too long; `@imports` and backtick paths that point
    to files that do not exist (template folders are only noted, their paths are generated later)
  - .claude/settings.json / settings.local.json: invalid JSON; defaultMode = bypassPermissions; allow lists that are too
    broad or let dangerous commands and arbitrary interpreters run without a prompt; deny rules written as `**/…`
    (they only match inside the working directory); hooks that call scripts which do not exist
  - .claude/skills: folders without SKILL.md, skills nested under a group folder (not loaded), frontmatter missing
    name/description, project skills shadowing a global skill; .agents/skills present but not loaded
  - .mcp.json, .cursor/mcp.json, .codex/config.toml: MCP servers whose command is not installed on this machine
  - CLAUDE.md, AGENTS.md, .claude/**, .codex/**, .cursor/**: strings that look like leaked secrets (masked in output)
Exit code 1 when any error-level finding exists, else 0 — usable as a CI gate.
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

HOME = Path.home()
MANAGED_SKILL_DIRS = {"synced"}  # kept in sync by the Claude app, not user skills
NESTED_SKIP = {"node_modules", "vendor", ".git", "build", "dist", ".next", "Pods", "storage", "target", "venv", ".venv"}
TEXT_EXT = {".md", ".json", ".sh", ".py", ".js", ".ts", ".toml", ".yaml", ".yml", ".txt", ""}
SECRET_PATTERNS = [
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[abpr]-[A-Za-z0-9-]{10,}")),
    ("Private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assign", re.compile(r"(?i)(api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"'][^\"'\s$<{]{16,}[\"']")),
]
RISKY_ALLOW = re.compile(r"^Bash\((?:git push|rm -rf|sudo|(?:npx|node|python3?|tsx|bash|sh|zsh|deno|bun)(?: tsx)?)[ :]?\*\)$")
PATH_TOKEN = re.compile(r"`((?:\./|\.\./|~/)?[\w.@-]+(?:/[\w.@-]+)+/?|[\w.-]+\.(?:md|sh|py|js|ts|json|toml|yaml|yml))`")
IMPORT = re.compile(r"^@(\S+)\s*$", re.M)

MESSAGES = {
    "en": {
        "too_long": "{n} lines, too long (>{max}); agents skip rules in long files",
        "import_template": "import `@{imp}` not present in the template (generated when used)",
        "import_missing": "import `@{imp}` points to a file that does not exist",
        "paths_missing": "{n} path(s) not found: {shown}",
        "json_broken": "invalid JSON: {err}",
        "bypass": "defaultMode = bypassPermissions (the agent runs every command without asking)",
        "broad_allow": "allow list too broad: {items}",
        "risky_allow": "dangerous commands or arbitrary interpreters allowed without a prompt: {items}",
        "weak_deny": "deny `**/…` only matches inside the working directory; write `//**/…` to block everywhere: {items}",
        "hook_missing": "hook {event} calls a script that does not exist: `{first}`",
        "skill_nested": "skills nested under a group folder, Claude Code does not load {n}: {names}",
        "skill_no_md": "skill folder without SKILL.md",
        "skill_no_fm": "SKILL.md has no frontmatter",
        "skill_fm_key": "frontmatter missing `{key}`",
        "skill_dup_global": "same name as the global skill `~/.claude/skills/{name}`",
        "secret": "possible secret ({label}): `{masked}`",
        "secret_assign": "secret assignment, check by hand",
        "agents_skills": "{n} skills in .agents/skills are not loaded by Claude Code (only .claude/skills/); symlink `.claude/skills -> ../.agents/skills` or list them in CLAUDE.md",
        "mcp_missing": "MCP `{name}` runs `{exe}` but this machine has no such command (Docker? fix the command or document the setup)",
        "title": "# Agent lint — {date}",
        "summary": "Scanned **{n}** root(s): 🔴 {e} errors · 🟡 {w} warnings · 🔵 {i} notes.",
        "none": "No findings.",
        "table": "| | File | Finding |",
        "global": "~/.claude (global)",
    },
    "vi": {
        "too_long": "{n} dòng, quá dài (>{max}); agent dễ bỏ sót luật",
        "import_template": "import `@{imp}` chưa có trong template (sinh khi dùng)",
        "import_missing": "import `@{imp}` trỏ tới file không tồn tại",
        "paths_missing": "{n} đường dẫn không thấy: {shown}",
        "json_broken": "JSON hỏng: {err}",
        "bypass": "defaultMode = bypassPermissions (agent chạy mọi lệnh không hỏi)",
        "broad_allow": "quyền allow quá rộng: {items}",
        "risky_allow": "cho chạy không cần hỏi lệnh nguy hiểm hoặc trình thông dịch bất kỳ: {items}",
        "weak_deny": "deny dạng `**/…` chỉ khớp trong thư mục làm việc; viết `//**/…` để chặn mọi nơi: {items}",
        "hook_missing": "hook {event} gọi script không tồn tại: `{first}`",
        "skill_nested": "skill lồng trong thư mục nhóm, Claude Code không nạp {n} skill: {names}",
        "skill_no_md": "thư mục skill thiếu SKILL.md",
        "skill_no_fm": "SKILL.md thiếu frontmatter",
        "skill_fm_key": "frontmatter thiếu `{key}`",
        "skill_dup_global": "trùng tên với skill toàn cục `~/.claude/skills/{name}`",
        "secret": "nghi lộ secret ({label}): `{masked}`",
        "secret_assign": "gán secret, cần xem tay",
        "agents_skills": "{n} skill trong .agents/skills Claude Code không tự nạp (chỉ nạp .claude/skills/); symlink `.claude/skills -> ../.agents/skills` hoặc liệt kê trong CLAUDE.md",
        "mcp_missing": "MCP `{name}` gọi `{exe}` nhưng máy không có lệnh này (chạy trong Docker? sửa command hoặc ghi rõ cách cài)",
        "title": "# Lint agent — {date}",
        "summary": "Đã soát **{n}** nơi: 🔴 {e} lỗi · 🟡 {w} cảnh báo · 🔵 {i} ghi chú.",
        "none": "Không phát hiện vấn đề nào.",
        "table": "| | File | Vấn đề |",
        "global": "~/.claude (toàn cục)",
    },
}


class Linter:
    def __init__(self, lang="en", max_lines=300, home_claude=None):
        self.m = MESSAGES.get(lang, MESSAGES["en"])
        self.max_lines = max_lines
        self.items = []  # (level, scope, file, message)
        self.claude_home = home_claude or Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
        self.global_names = set(skill_names(self.claude_home / "skills"))

    def t(self, msg_id, **kw):
        return self.m[msg_id].format(**kw)

    def add(self, level, scope, file, msg):
        self.items.append((level, scope, str(file), msg))

    # --- checks -------------------------------------------------------------------------------------------------
    def instructions(self, f, root, scope):
        text = f.read_text(errors="ignore")
        n = text.count("\n") + 1
        in_template = any("template" in part for part in f.relative_to(root).parts[:-1])
        if n > self.max_lines:
            self.add("warn", scope, rel(f, root), self.t("too_long", n=n, max=self.max_lines))
        for imp in IMPORT.findall(text):
            if not resolve(imp, f.parent, root):
                if in_template:
                    self.add("info", scope, rel(f, root), self.t("import_template", imp=imp))
                else:
                    self.add("error", scope, rel(f, root), self.t("import_missing", imp=imp))
        missing = sorted({tok for tok in PATH_TOKEN.findall(text) if looks_broken(tok, f.parent, root)})
        if missing:
            shown = ", ".join(f"`{x}`" for x in missing[:8]) + (" …" if len(missing) > 8 else "")
            self.add("info" if in_template else "warn", scope, rel(f, root), self.t("paths_missing", n=len(missing), shown=shown))

    def settings(self, f, root, scope):
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            self.add("error", scope, rel(f, root), self.t("json_broken", err=e))
            return
        perms = data.get("permissions", {}) or {}
        if perms.get("defaultMode") == "bypassPermissions":
            self.add("warn", scope, rel(f, root), self.t("bypass"))
        allow = perms.get("allow", []) or []
        broad = [a for a in allow if a in ("Bash", "Bash(*)", "Bash(:*)", "*")]
        if broad:
            self.add("warn", scope, rel(f, root), self.t("broad_allow", items=", ".join(broad)))
        risky = [a for a in allow if RISKY_ALLOW.match(a)]
        if risky:
            self.add("warn", scope, rel(f, root), self.t("risky_allow", items=", ".join(f"`{a}`" for a in risky[:6])))
        weak = [d for d in perms.get("deny", []) or [] if re.match(r"^(Read|Edit|Write)\(\*\*/", d)]
        if weak:
            self.add("warn", scope, rel(f, root), self.t("weak_deny", items=", ".join(f"`{d}`" for d in weak[:4])))
        for event, groups in (data.get("hooks") or {}).items():
            for g in groups or []:
                for h in g.get("hooks", []) or []:
                    cmd = h.get("command", "")
                    first = cmd.strip().split()[0] if cmd.strip() else ""
                    first = first.replace('"', "").replace("'", "")
                    first = first.replace("${CLAUDE_PROJECT_DIR}", str(root)).replace("$CLAUDE_PROJECT_DIR", str(root))
                    if first and "/" in first and not Path(first).expanduser().exists():
                        self.add("error", scope, rel(f, root), self.t("hook_missing", event=event, first=first))

    def skills(self, skills_dir, root, scope, check_global_dup):
        if not skills_dir.is_dir():
            return
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name in MANAGED_SKILL_DIRS:
                continue
            sk = d / "SKILL.md"
            if not sk.is_file():
                nested = sorted(x.parent.name for x in d.glob("*/SKILL.md"))
                if nested:
                    self.add("error", scope, rel(d, root), self.t("skill_nested", n=len(nested), names=", ".join(nested[:6])))
                else:
                    self.add("error", scope, rel(d, root), self.t("skill_no_md"))
                continue
            head = sk.read_text(errors="ignore")[:2000]
            if not head.startswith("---"):
                self.add("error", scope, rel(sk, root), self.t("skill_no_fm"))
            else:
                fm = head.split("---")[1] if head.count("---") >= 2 else ""
                for key in ("name", "description"):
                    if not re.search(rf"^{key}\s*:", fm, re.M):
                        self.add("error", scope, rel(sk, root), self.t("skill_fm_key", key=key))
            if check_global_dup and d.name in self.global_names:
                self.add("info", scope, rel(d, root), self.t("skill_dup_global", name=d.name))

    def secrets(self, files, root, scope):
        for f in files:
            try:
                if f.suffix not in TEXT_EXT or f.stat().st_size > 1_000_000:
                    continue
                text = f.read_text(errors="ignore")
            except OSError:
                continue
            for label, pat in SECRET_PATTERNS:
                m = pat.search(text)
                if m:
                    line = text[: m.start()].count("\n") + 1
                    level = "warn" if label == "assign" else "error"
                    shown = self.t("secret_assign") if label == "assign" else label
                    self.add(level, scope, f"{rel(f, root)}:{line}", self.t("secret", label=shown, masked=mask(m.group(0))))
                    break

    def mcp(self, root, scope):
        for f, name, cmd in mcp_commands(root):
            exe = cmd.split()[0]
            found = Path(exe).expanduser().exists() if "/" in exe else shutil.which(exe)
            if not found:
                self.add("warn", scope, rel(f, root), self.t("mcp_missing", name=name, exe=exe))

    def root(self, root, scope, is_global=False):
        root = Path(root)
        for f in instruction_files(root, is_global):
            self.instructions(f, root, scope)
        if not is_global:
            self.mcp(root, scope)
        cdir = root if is_global else root / ".claude"
        for name in ("settings.json", "settings.local.json"):
            f = cdir / name
            if f.is_file():
                self.settings(f, root, scope)
        self.skills(cdir / "skills", root, scope, check_global_dup=not is_global)
        agents_skills = root / ".agents" / "skills"
        if not is_global and agents_skills.is_dir() and not (root / ".claude" / "skills").exists():
            n = len(list(agents_skills.glob("*/SKILL.md")))
            if n:
                self.add("warn", scope, ".agents/skills", self.t("agents_skills", n=n))
        self.secrets(agent_files(root, include_skills=not is_global), root, scope)

    # --- output -------------------------------------------------------------------------------------------------
    def counts(self):
        return {k: sum(1 for i in self.items if i[0] == k) for k in ("error", "warn", "info")}

    def markdown(self, scanned):
        icon = {"error": "🔴", "warn": "🟡", "info": "🔵"}
        c = self.counts()
        lines = [self.t("title", date=date.today()), "", self.t("summary", n=scanned, e=c["error"], w=c["warn"], i=c["info"]), ""]
        if not self.items:
            return "\n".join(lines + [self.t("none")]) + "\n"
        order = {"error": 0, "warn": 1, "info": 2}
        by_scope = {}
        for it in sorted(self.items, key=lambda i: (order[i[0]], i[1], i[2])):
            by_scope.setdefault(it[1], []).append(it)
        for scope in sorted(by_scope, key=lambda s: min(order[i[0]] for i in by_scope[s])):
            lines += [f"## {scope}", "", self.t("table"), "|---|---|---|"]
            for level, _, f, msg in by_scope[scope]:
                lines.append(f"| {icon[level]} | `{f}` | {msg} |")
            lines.append("")
        return "\n".join(lines)

    def json(self, scanned):
        return {"scanned": scanned, "counts": self.counts(),
                "findings": [{"level": lv, "scope": sc, "file": f, "message": m} for lv, sc, f, m in self.items]}


# --- helpers -------------------------------------------------------------------------------------------------------
def rel(p, base):
    try:
        return str(Path(p).relative_to(base))
    except ValueError:
        return str(p).replace(str(HOME), "~")


def mask(s):
    return s[:6] + "…" + s[-2:] if len(s) > 10 else "…"


def resolve(token, *bases):
    token = token.rstrip("/")
    if token.startswith("~/"):
        return Path(token).expanduser().exists()
    return any((b / token).exists() for b in bases)


def looks_broken(token, *bases):
    """Only report when it is surely a path: has '/', is not a package/alias/placeholder, and its first folder exists."""
    if "/" not in token or any(c in token for c in "<*{$") or re.search(r"YYYY|XXX|\.\.\.", token) or token.startswith(("http", "/", "@")):
        return False
    if token.startswith("~/"):
        return not Path(token).expanduser().exists()
    parts = [x for x in token.split("/") if x not in (".", "")]
    if not parts or parts[0] == "..":
        return False
    anchored = [b for b in bases if (b / parts[0]).exists()]
    return bool(anchored) and not any((b / token.rstrip("/")).exists() for b in anchored)


def skill_names(skills_dir):
    out = {}
    if not skills_dir.is_dir():
        return out
    for d in sorted(skills_dir.iterdir()):
        sk = d / "SKILL.md"
        if d.is_dir() and sk.is_file():
            out[d.name] = sk
    return out


def agent_files(root, include_skills=True):
    files = [root / n for n in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".mcp.json") if (root / n).is_file()]
    skip = {"node_modules", "plugins", "projects", "cache", "file-history", "shell-snapshots", "telemetry", "debug",
            "paste-cache", "session-env", "sessions", "backups", "todos", "statsig", "ide", "tasks", "teams", "jobs"}
    for d in (".claude", ".codex", ".cursor"):
        p = root / d
        if p.is_dir():
            for f in p.rglob("*"):
                parts = f.relative_to(p).parts
                if not f.is_file() or any(x in skip for x in parts):
                    continue
                if not include_skills and parts and parts[0] == "skills":
                    continue
                files.append(f)
    return files


def instruction_files(root, is_global):
    """CLAUDE.md / AGENTS.md at the root and up to two folders deep (agents load nested files when working there)."""
    out = [root / n for n in ("CLAUDE.md", "AGENTS.md") if (root / n).is_file()]
    if is_global:
        return out
    for pattern in ("*/", "*/*/"):
        for d in root.glob(pattern):
            if any(x.startswith(".") or x in NESTED_SKIP for x in d.relative_to(root).parts):
                continue
            out += [d / n for n in ("CLAUDE.md", "AGENTS.md") if (d / n).is_file()]
    return out


def mcp_commands(root):
    """(file, server, command) from .mcp.json, .cursor/mcp.json and .codex/config.toml."""
    for f in (root / ".mcp.json", root / ".cursor" / "mcp.json"):
        if f.is_file():
            try:
                servers = json.loads(f.read_text()).get("mcpServers", {})
            except (json.JSONDecodeError, AttributeError):
                continue
            for name, cfg in servers.items():
                if isinstance(cfg, dict) and cfg.get("command"):
                    yield f, name, cfg["command"]
    toml = root / ".codex" / "config.toml"
    if toml.is_file():
        server = None
        for line in toml.read_text().splitlines():
            m = re.match(r"\s*\[mcp_servers\.([^\]]+)\]", line)
            if m:
                server = m.group(1)
            m = re.match(r"\s*command\s*=\s*[\"']([^\"']+)", line)
            if m and server:
                yield toml, server, m.group(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-lint", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("paths", nargs="*", help="project roots (default: current directory)")
    ap.add_argument("--global", dest="global_", action="store_true", help="also lint ~/.claude ($CLAUDE_CONFIG_DIR)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="write the markdown report here")
    ap.add_argument("--lang", choices=sorted(MESSAGES), default="en")
    ap.add_argument("--max-lines", type=int, default=300)
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    a = ap.parse_args(argv)
    lint = Linter(a.lang, a.max_lines)
    targets = [Path(p).expanduser().resolve() for p in a.paths] or ([] if a.global_ else [Path.cwd()])
    scanned = 0
    if a.global_:
        lint.root(lint.claude_home, lint.t("global"), is_global=True)
        scanned += 1
    for t in targets:
        if not t.is_dir():
            print(f"not a directory: {t}", file=sys.stderr)
            continue
        lint.root(t, str(t).replace(str(HOME), "~"))
        scanned += 1
    md = lint.markdown(scanned)
    if a.out:
        Path(a.out).write_text(md)
    if a.json:
        print(json.dumps(lint.json(scanned), ensure_ascii=False, indent=1))
    elif a.quiet or a.out:
        print(md.split("\n")[2])
    else:
        print(md, end="")
    return 1 if lint.counts()["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
