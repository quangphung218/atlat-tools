#!/usr/bin/env python3
"""recall — long-term memory for a working folder: full-text search (SQLite FTS5) over your markdown notes and your Claude Code
conversations, newest first. No LLM, no daemon, no network. Diacritics-insensitive (Vietnamese "trien khai" matches "triển khai").

    recall "deploy failed"              search (index is refreshed first), 10 newest hits
    recall "a b" -n 20                  more hits · words are ANDed; "exact phrase"; a OR b; prefix*
    recall "x" --source chat            one source only: chat | notes | memory
    recall --index                      refresh the index only (safe to call from a hook)
    recall --stats                      documents per source and database size

What gets indexed (configure with flags or ~/.config/recall/config.json):
  - chat: Claude Code transcripts of the current project (~/.claude/projects/<key of cwd>/*.jsonl): your messages and the
    assistant's replies, tool calls and thinking skipped; --all-projects indexes every project
  - notes: markdown/text files under --dir folders (default: the current directory, depth-limited, skipping node_modules etc.);
    `--dir ~/runs:run` gives that folder its own source label
  - memory: the project's auto-memory folder (~/.claude/projects/<key>/memory)
The index lives in --db (default ~/.local/share/recall/<key>.db) and only re-reads files whose mtime/size changed.
Credentials never enter the index: private-key blocks, API keys, tokens, JWTs and "password/pass/pw/pin: …" values are masked
before insert, and a file with three or more such spans (a credential note) is skipped entirely (--no-redact disables both). Skip paths with --skip-glob or a .recallignore file (one glob per line) at a --dir root.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
# Secrets never enter the index: keys, tokens, PEM blocks, JWTs and "password: …" assignments are masked before insert.
SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<redacted private key>"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), "<redacted key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "<redacted token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted key>"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), "<redacted token>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<redacted key>"),                 # Google API key
    (re.compile(r"\bAQ\.[A-Za-z0-9_-]{30,}"), "<redacted key>"),                   # Google AI Studio key
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"), "<redacted key>"),        # Stripe
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "<redacted token>"),                     # Hugging Face
    # a line that mentions a key/token/secret and then carries a long opaque string: mask the string
    (re.compile(r"(?i)(\b(?:api[_ -]?key|key|token|secret|credential|bearer)\b[^\n]{0,40}?)\b([A-Za-z0-9._-]{24,})"), r"\1<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+)?"), "<redacted jwt>"),
    # "password: x", "pass gpt: x", "mk: x", "pin=1234", "user|pass" pairs — the key stays, the value goes
    (re.compile(r"(?i)(\b(?:pass(?:word|wd)?|pw|mk|mật khẩu|pin|otp|secret|token|api[_ -]?key|access[_ -]?key|private[_ -]?key)\b[^\n:=]{0,12}[:=]\s*[\"']?)([^\s\"']{4,})"), r"\1<redacted>"),
    (re.compile(r"([\w.+-]+@[\w.-]+\.\w+)\s*[|/]\s*(\S{4,})"), r"\1|<redacted>"),
]
SECRET_NOTE_THRESHOLD = 3  # a file with this many masked spans is a credential note: it is not indexed at all
IGNORE_FILE = ".recallignore"
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
TAGS = re.compile(r"</?(command-[a-z-]+|local-command-[a-z]+)>")
CHUNK = 1800
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "vendor", "Pods", ".venv", "venv", "__pycache__", "target", "coverage"}
NOTE_EXT = {".md", ".txt", ".markdown", ".rst"}
NAMES = {"en": {"user": "user", "assistant": "assistant", "summary": "summary", "none": "no match", "index": "index: {changed} files updated, {gone} removed"},
         "vi": {"user": "anh", "assistant": "em", "summary": "tóm tắt", "none": "không thấy gì", "index": "chỉ mục: {changed} file cập nhật, {gone} file gỡ"}}


def redact(text):
    """Mask anything that looks like a credential; returns (text, number of masked spans)."""
    n = 0
    for pat, rep in SECRETS:
        text, k = pat.subn(rep, text)
        n += k
    return text, n


def ignore_patterns(root):
    """Glob patterns from <root>/.recallignore (one per line, # comments), matched against paths relative to root."""
    f = Path(root) / IGNORE_FILE
    if not f.is_file():
        return []
    return [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")]


def project_key(path):
    """Claude Code's folder name for a project: the absolute path with '/' replaced by '-'."""
    return "-" + str(Path(path).resolve()).strip("/").replace("/", "-")


def local_ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16].replace("T", " ")


class Recall:
    def __init__(self, cwd=None, db=None, dirs=None, claude_dir=None, all_projects=False, depth=4, lang="en", redact_secrets=True, skip_globs=()):
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self.claude = Path(claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
        self.key = project_key(self.cwd)
        self.db_path = Path(db) if db else HOME / ".local" / "share" / "recall" / f"{self.key.strip('-')[-80:]}.db"
        # a dir may carry a label: "~/notes:notes" → source name "notes"; default label is "notes"
        self.dirs = []
        for d in (dirs or [str(self.cwd)]):
            d = str(d)
            path, label = (d.rsplit(":", 1) if ":" in d and not d.endswith(":") and "/" not in d.rsplit(":", 1)[1] else (d, "notes"))
            self.dirs.append((Path(path).expanduser().resolve(), label))
        self.all_projects = all_projects
        self.depth = depth
        self.redact_secrets = redact_secrets
        self.skip_globs = list(skip_globs)
        self.changed_paths = []   # (source, path) re-read in the last index()
        self.redacted = 0         # masked spans in the last index()
        self.unreadable = []      # files the OS refused to read (e.g. iCloud placeholders not downloaded yet)
        self.secret_files = []    # files skipped because they are mostly credentials
        self.n = NAMES.get(lang, NAMES["en"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER);
            CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(text, source UNINDEXED, path UNINDEXED, ts UNINDEXED, who UNINDEXED,
                tokenize = 'unicode61 remove_diacritics 2');""")

    # --- sources -----------------------------------------------------------------------------------------------
    def sources(self):
        projects = self.claude / "projects"
        if self.all_projects and projects.is_dir():
            for d in sorted(projects.iterdir()):
                for p in sorted(d.glob("*.jsonl")):
                    yield "chat", p
        else:
            for p in sorted((projects / self.key).glob("*.jsonl")):
                yield "chat", p
        for p in sorted((projects / self.key / "memory").glob("*.md")):
            yield "memory", p
        for root, label in self.dirs:
            for p in self.walk(root):
                yield label, p

    def walk(self, root):
        import fnmatch
        base_depth = len(root.parts)
        import unicodedata
        globs = [unicodedata.normalize("NFC", g) for g in self.skip_globs + ignore_patterns(root)]  # cloud drives hand back NFD names

        def skip(rel):
            rel = unicodedata.normalize("NFC", rel)
            parts = rel.split("/")
            return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(parts[-1], g) or any(fnmatch.fnmatch(part, g) for part in parts) for g in globs)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") and not skip(str((Path(dirpath) / d).relative_to(root)))]
            if len(Path(dirpath).parts) - base_depth >= self.depth:
                dirnames[:] = []
            for f in filenames:
                p = Path(dirpath) / f
                if p.suffix.lower() in NOTE_EXT and p.stat().st_size < 2_000_000 and not skip(str(p.relative_to(root))):
                    yield p

    def chat_rows(self, path):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                kind = d.get("type")
                if kind not in ("user", "assistant") or d.get("isSidechain"):
                    continue
                content = (d.get("message") or {}).get("content")
                if isinstance(content, str):
                    parts = [content]
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                else:
                    continue
                text = TAGS.sub("", REMINDER.sub("", "\n".join(parts))).strip()
                if len(text) < 3:
                    continue
                if self.redact_secrets:
                    text, k = redact(text)
                    self.redacted += k
                who = self.n["summary"] if d.get("isCompactSummary") else (self.n["user"] if kind == "user" else self.n["assistant"])
                yield local_ts(d.get("timestamp") or ""), who, text

    def file_rows(self, path):
        text = path.read_text(encoding="utf-8", errors="replace")
        if self.redact_secrets:
            text, k = redact(text)
            self.redacted += k
        ts = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        buf = ""
        for para in re.split(r"\n\s*\n", text):
            if buf and len(buf) + len(para) > CHUNK:
                yield ts, "", buf
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            yield ts, "", buf

    # --- index + search ----------------------------------------------------------------------------------------
    def index(self):
        db = self.db
        seen, changed = set(), 0
        self.changed_paths, self.redacted, self.unreadable, self.secret_files = [], 0, [], []
        known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM files")}
        for source, p in self.sources():
            key = str(p)
            seen.add(key)
            try:
                st = p.stat()
            except OSError:
                continue
            if known.get(key) == (st.st_mtime, st.st_size):
                continue
            before = self.redacted
            try:
                rows = list(self.chat_rows(p) if source == "chat" else self.file_rows(p))
            except OSError:  # iCloud/Dropbox placeholder not downloaded yet (EDEADLK on macOS), permission, vanished file
                self.unreadable.append(key)
                continue
            db.execute("DELETE FROM docs WHERE path = ?", (key,))
            if source != "chat" and self.redact_secrets and self.redacted - before >= SECRET_NOTE_THRESHOLD:
                # a credential note: remember we saw it (so it is not re-read every run) but index nothing from it
                self.secret_files.append(key)
                db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (key, st.st_mtime, st.st_size))
                continue
            db.executemany("INSERT INTO docs(text, source, path, ts, who) VALUES (?, ?, ?, ?, ?)", ((t, source, key, ts, who) for ts, who, t in rows))
            db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?)", (key, st.st_mtime, st.st_size))
            changed += 1
            self.changed_paths.append((source, key))
        gone = set(known) - seen
        for g in gone:
            db.execute("DELETE FROM docs WHERE path = ?", (g,))
            db.execute("DELETE FROM files WHERE path = ?", (g,))
        db.commit()
        return changed, len(gone)

    def short(self, path):
        p = path.replace(str(self.cwd) + "/", "").replace(str(HOME), "~")
        return re.sub(r"projects/[^/]+/", "projects/…/", p)

    def search(self, query, limit=10, source=None):
        sql = ("SELECT ts, source, who, path, snippet(docs, 0, '«', '»', ' … ', 24) FROM docs WHERE docs MATCH ? "
               + ("AND source = ? " if source else "") + "ORDER BY ts DESC LIMIT ?")
        args = [query] + ([source] if source else []) + [limit]
        try:
            return self.db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            args[0] = " ".join('"' + w.replace('"', "") + '"' for w in query.split())
            return self.db.execute(sql, args).fetchall()

    def stats(self):
        return list(self.db.execute("SELECT source, count(*) FROM docs GROUP BY source ORDER BY 2 DESC")), self.db_path.stat().st_size // 1024


def load_config():
    f = HOME / ".config" / "recall" / "config.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def main(argv=None):
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="recall", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("query", nargs="*")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--source", help="chat | notes | memory | a label given with --dir path:label")
    ap.add_argument("--dir", action="append", default=cfg.get("dirs"), help="note folder to index, optionally path:label (repeatable; default: current directory)")
    ap.add_argument("--depth", type=int, default=cfg.get("depth", 4))
    ap.add_argument("--all-projects", action="store_true", default=cfg.get("all_projects", False))
    ap.add_argument("--db", default=cfg.get("db"))
    ap.add_argument("--claude-dir", default=cfg.get("claude_dir"))
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--skip-glob", action="append", default=cfg.get("skip_globs", []), help="skip files/folders matching this glob (repeatable; also read from <dir>/.recallignore)")
    ap.add_argument("--no-redact", action="store_true", help="index credentials verbatim (default: keys, tokens, PEM blocks, passwords are masked)")
    ap.add_argument("--changed", action="store_true", help="print the files (re)indexed by this run")
    ap.add_argument("--lang", choices=sorted(NAMES), default=cfg.get("lang", "en"))
    a = ap.parse_args(argv)
    r = Recall(None, a.db, a.dir, a.claude_dir, a.all_projects, a.depth, a.lang, not a.no_redact, a.skip_glob)
    changed, gone = r.index()
    if not (a.quiet or a.query):
        print(r.n["index"].format(changed=changed, gone=gone) + (f" · redacted {r.redacted}" if r.redacted else "") + (f" · credential notes skipped {len(r.secret_files)}" if r.secret_files else "") + (f" · unreadable {len(r.unreadable)}" if r.unreadable else ""))
    if a.changed:
        for src, path in r.changed_paths:
            print(f"{src}\t{path}")
    if a.stats:
        rows, kb = r.stats()
        for src, n in rows:
            print(f"{src:7} {n}")
        print(f"DB: {kb} KB · {r.db_path}")
    if a.query:
        hits = r.search(" ".join(a.query), a.n, a.source)
        if not hits:
            print(r.n["none"]); return 1
        for ts, src, who, path, snip in hits:
            print(f"{ts}  [{src + '/' + who if who else src}]  {r.short(path)}")
            print("   " + " ".join(snip.split()))
    elif not (a.index or a.stats):
        ap.print_help(); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
