#!/usr/bin/env python3
"""vn-style — linter for the Vietnamese "house voice" of replies, reports and chat messages: pronouns, filler openers and
closers, padding words, vague time estimates, spaces before punctuation, old-style tone marks, Title Case. Deterministic, no
LLM. Defaults encode a terse conclusion-first style (em/anh, no "bạn"/"tôi", new-style marks like `hoà`/`thuỷ`); bring your
own rules with --rules.

    vn-style reply.md                    report findings with line numbers
    vn-style -                           read stdin (pipe a message through before sending it)
    vn-style reply.md --fix              apply the safe fixes in place: NFC, no space before . , ; : ! ?, old → new tone marks
    vn-style docs/ --strict              exit 1 on warnings too (default: only errors, i.e. forbidden pronouns)
    vn-style x.md --json | --lang en
    vn-style --rules my-voice.json       JSON with any of: pronouns, openers, closers, fillers, hedges, vague_time, title_case,
                                         new_marks, first_line_announce (lists of strings or booleans)

Fixes never touch wording, only mechanics. Everything else is a report, because "có lẽ" can be a real hedge and "việc" can be a
noun — a human (or the agent that wrote the text) decides.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

DEFAULT_RULES = {
    "pronouns": ["bạn", "tôi", "mình"],
    "openers": ["Dưới đây là", "Chắc chắn rồi", "Tất nhiên", "Em sẽ tiến hành", "Để em", "Như đã nói", "Trước tiên,"],
    "closers": ["Hy vọng", "Tóm lại", "Nếu anh cần gì thêm", "Còn gì nữa không", "Chúc anh"],
    "fillers": ["tiến hành", "thực hiện việc", "một cách", "việc thực hiện", "nhằm mục đích", "trong trường hợp"],
    "hedges": ["có lẽ", "có thể là", "hình như", "dường như"],
    "vague_time": ["hơi lâu", "một chút", "khá lâu", "một lúc", "sớm thôi", "nhanh thôi"],
    "title_case": True,
    "new_marks": True,
    "first_line_announce": ["Em sẽ", "Em đang", "Em xin", "Để em"],
}
# tone marks (NFD): grave, acute, tilde, hook above, dot below
TONES = "̣̀́̃̉"
# old-style placement: mark on o/u of oa / oe / uy at the end of a syllable (hòa → hoà, thủy → thuỷ, khỏe → khoẻ)
OLD_MARK = re.compile(rf"([oO])([{TONES}])([aAeE])(?![\w\u0300-\u036f])|([uU])([{TONES}])([yY])(?![\w\u0300-\u036f])")
TOKEN = re.compile(r"[^\W\d_][\w\u0300-\u036f]*")
SPACE_PUNCT = re.compile(r" +([.,;:!?])(?=\s|$)")
TEXT = {"en": {"pronoun": "forbidden pronoun “{w}”", "opener": "filler opener “{w}”", "closer": "filler closer “{w}”", "filler": "padding word “{w}”",
               "hedge": "hedge “{w}” (keep only if the uncertainty is real)", "vague": "vague time estimate “{w}” (say a number)", "space": "space before punctuation",
               "old": "old-style tone mark “{w}” → “{new}”", "title": "Title Case: “{w}”", "nfc": "text is not NFC-normalised", "first": "first line announces (“{w}…”) instead of concluding",
               "sum": "{files} file(s): {e} errors, {w} warnings", "fixed": "fixed {n} file(s)", "clean": "clean"},
        "vi": {"pronoun": "đại từ không dùng “{w}”", "opener": "câu đệm mở đầu “{w}”", "closer": "câu đệm kết “{w}”", "filler": "từ thừa “{w}”",
               "hedge": "rào đón “{w}” (chỉ giữ khi thật sự chưa chắc)", "vague": "ước lượng mơ hồ “{w}” (ghi con số)", "space": "dấu cách trước dấu câu",
               "old": "dấu kiểu cũ “{w}” → “{new}”", "title": "Viết Hoa Kiểu Tiêu Đề: “{w}”", "nfc": "văn bản chưa chuẩn NFC", "first": "dòng đầu báo việc sẽ làm (“{w}…”) thay vì kết luận",
               "sum": "{files} file: {e} lỗi, {w} cảnh báo", "fixed": "đã sửa {n} file", "clean": "sạch"}}


def modern_marks(text):
    """Move the tone mark from o/u to the second vowel in open oa/oe/uy syllables; returns NFC text."""
    nfd = unicodedata.normalize("NFD", text)
    out = OLD_MARK.sub(lambda m: (m.group(1) + m.group(3) + m.group(2)) if m.group(1) else (m.group(4) + m.group(6) + m.group(5)), nfd)
    return unicodedata.normalize("NFC", out)


def title_runs(line, min_words=3):
    """Runs of 3+ consecutive words that each start with a capital and continue in lowercase (Title Case), skipping the sentence start."""
    runs, cur = [], []
    for m in TOKEN.finditer(line):
        w = m.group(0)
        titled = len(w) > 1 and w[0].isupper() and w[1:] == w[1:].lower()
        if titled:
            cur.append(w)
        else:
            if len(cur) >= min_words:
                runs.append(" ".join(cur))
            cur = []
    if len(cur) >= min_words:
        runs.append(" ".join(cur))
    return runs


def word_re(w):
    return re.compile(rf"(?<![\ẁ-ͯ]){re.escape(w)}(?![\ẁ-ͯ])", re.I)


class Linter:
    def __init__(self, rules=None, lang="en"):
        self.r = {**DEFAULT_RULES, **(rules or {})}
        self.t = TEXT.get(lang, TEXT["en"])

    def lint(self, text):
        """[(line, level, code, message)]"""
        out = []
        if unicodedata.normalize("NFC", text) != text:
            out.append((1, "warn", "nfc", self.t["nfc"]))
        lines = text.split("\n")
        in_code = False
        first_seen = False
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or not line.strip():
                continue
            plain = re.sub(r"`[^`]*`", "", line)
            if not first_seen and not line.startswith("#"):
                first_seen = True
                for w in self.r.get("first_line_announce") or []:
                    if plain.lstrip("*- ").startswith(w):
                        out.append((i, "warn", "first", self.t["first"].format(w=w)))
            for w in self.r.get("pronouns") or []:
                if word_re(w).search(plain):
                    out.append((i, "error", "pronoun", self.t["pronoun"].format(w=w)))
            for code, key in (("opener", "openers"), ("closer", "closers"), ("filler", "fillers"), ("hedge", "hedges"), ("vague", "vague_time")):
                for w in self.r.get(key) or []:
                    if word_re(w).search(plain):
                        out.append((i, "warn", code, self.t[code].format(w=w)))
            if SPACE_PUNCT.search(plain):
                out.append((i, "warn", "space", self.t["space"]))
            if self.r.get("new_marks"):
                nfd = unicodedata.normalize("NFD", plain)
                for m in OLD_MARK.finditer(nfd):
                    start = nfd.rfind(" ", 0, m.start()) + 1
                    end = nfd.find(" ", m.end())
                    word = nfd[start:end if end > 0 else None]
                    out.append((i, "warn", "old", self.t["old"].format(w=unicodedata.normalize("NFC", word), new=modern_marks(word))))
            if self.r.get("title_case") and not plain.lstrip().startswith(("#", "|")):
                for run in title_runs(plain):
                    out.append((i, "warn", "title", self.t["title"].format(w=run)))
        return out

    def fix(self, text):
        text = unicodedata.normalize("NFC", text)
        parts = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.S)  # leave code blocks and inline code alone
        for k in range(0, len(parts), 2):
            seg = SPACE_PUNCT.sub(r"\1", parts[k])
            if self.r.get("new_marks"):
                seg = modern_marks(seg)
            parts[k] = seg
        return "".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vn-style", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("paths", nargs="+", help="files, folders (.md/.txt) or - for stdin")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", help="JSON file overriding the default rule lists")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    a = ap.parse_args(argv)
    rules = json.loads(Path(a.rules).read_text()) if a.rules else None
    lint = Linter(rules, a.lang)
    files = []
    for p in a.paths:
        if p == "-":
            files.append(("-", sys.stdin.read()))
            continue
        pp = Path(p)
        if pp.is_dir():
            files += [(str(f), f.read_text(encoding="utf-8", errors="replace")) for f in sorted(pp.rglob("*")) if f.suffix in (".md", ".txt") and f.is_file()]
        elif pp.is_file():
            files.append((str(pp), pp.read_text(encoding="utf-8", errors="replace")))
        else:
            print(f"not found: {p}", file=sys.stderr)
    findings, fixed = {}, 0
    for name, text in files:
        if a.fix:
            new = lint.fix(text)
            if name == "-":
                sys.stdout.write(new)
            elif new != text:
                Path(name).write_text(new, encoding="utf-8")
                fixed += 1
            text = new
        f = lint.lint(text)
        if f:
            findings[name] = f
    e = sum(1 for f in findings.values() for x in f if x[1] == "error")
    w = sum(1 for f in findings.values() for x in f if x[1] == "warn")
    # stdin + --fix: stdout carries only the fixed text (so it can be piped on); the report goes to stderr
    out = sys.stderr if (a.fix and any(n == "-" for n, _ in files)) else sys.stdout
    if a.json:
        print(json.dumps({"files": len(files), "errors": e, "warnings": w, "fixed": fixed,
                          "findings": [{"file": n, "line": l, "level": lv, "code": c, "message": m} for n, f in findings.items() for l, lv, c, m in f]}, ensure_ascii=False, indent=1), file=out)
    else:
        for name, f in findings.items():
            for line, level, code, msg in f:
                print(f"{name}:{line}: {'E' if level == 'error' else 'W'} {code}: {msg}", file=out)
        if a.fix and not any(n == "-" for n, _ in files):
            print(lint.t["fixed"].format(n=fixed), file=out)
        print(lint.t["sum"].format(files=len(files), e=e, w=w) if findings else lint.t["clean"], file=out)
    return 1 if e or (a.strict and w) else 0


if __name__ == "__main__":
    sys.exit(main())
