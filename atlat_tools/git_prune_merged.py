#!/usr/bin/env python3
"""git-prune-merged — delete remote (and optionally local) branches already merged into the main branch, with the safety
filters you would apply by hand: never main/master/develop/release*/prod*, never a branch with an open pull request, never a
branch checked out in any worktree. Lists first; deletes only with --delete.

    git-prune-merged                     list what would go (in the current repo)
    git-prune-merged ~/code/app --delete remote branches merged into origin/<main>
    git-prune-merged --delete --local    also delete merged local branches (off by default: bots and worktrees own local branches)
    git-prune-merged --base origin/develop --keep 'hotfix/*' --keep 'v*'

Merged = `git branch -r --merged <base>` after `git fetch --prune`; commits stay reachable from the base, so a deleted branch can
be recreated from its SHA. Open PRs come from `gh pr list` when `gh` is available (skipped silently otherwise).
"""
import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

KEEP_RE = re.compile(r"^(main|master|develop|dev|trunk|release.*|prod.*|HEAD)$")
TEXT = {"en": {"fetch": "git fetch failed: {err}", "keep_pr": "  keep {b} (open PR)", "keep_wt": "  keep {b} (checked out in a worktree)",
               "remote": "Remote branches merged into {base}, to delete: {n}", "local": "Local merged branches{how}: {n}",
               "local_list": " (listing only, add --local to delete)", "local_del": ", to delete", "dry": "\nNothing deleted. Add --delete to delete.",
               "done": "✓ done: deleted {r} remote and {l} local branches in {repo}"},
        "vi": {"fetch": "git fetch lỗi: {err}", "keep_pr": "  giữ {b} (PR đang mở)", "keep_wt": "  giữ {b} (đang checkout ở worktree)",
               "remote": "Branch remote đã merge vào {base}, sẽ xoá: {n}", "local": "Branch local đã merge{how}: {n}",
               "local_list": " (chỉ liệt kê, thêm --local để xoá)", "local_del": ", sẽ xoá", "dry": "\nChưa xoá gì. Thêm --delete để xoá thật.",
               "done": "✓ xong: xoá {r} branch remote, {l} local ở {repo}"}}


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def default_base(repo):
    head = git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False).strip()
    return head.replace("refs/remotes/", "") if head else "origin/main"


def open_prs(repo):
    if not shutil.which("gh"):
        return set()
    r = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "headRefName"], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        return set()
    try:
        return {p["headRefName"] for p in json.loads(r.stdout)}
    except ValueError:
        return set()


def checked_out(repo):
    return {l.split("refs/heads/", 1)[1] for l in git(repo, "worktree", "list", "--porcelain").splitlines() if l.startswith("branch refs/heads/")}


def plan(repo, base, keep_globs=(), lang="en"):
    t = TEXT.get(lang, TEXT["en"])
    git(repo, "fetch", "--prune", "-q", "origin")
    prs, wts, notes = open_prs(repo), checked_out(repo), []
    remote_name = base.split("/", 1)[0]

    def keep(b):
        return KEEP_RE.match(b) or any(fnmatch.fnmatch(b, g) for g in keep_globs)

    remote = []
    for line in git(repo, "branch", "-r", "--merged", base, "--format=%(refname:short)").splitlines():
        b = line.strip()
        if not b.startswith(remote_name + "/") or "->" in b:
            continue
        b = b[len(remote_name) + 1:]
        if not b or keep(b):
            continue
        if b in prs:
            notes.append(t["keep_pr"].format(b=b)); continue
        if b in wts:
            notes.append(t["keep_wt"].format(b=b)); continue
        remote.append(b)
    local = [b.strip() for b in git(repo, "branch", "--merged", base, "--format=%(refname:short)").splitlines()
             if b.strip() and not keep(b.strip()) and b.strip() not in wts]
    return remote, local, notes


def main(argv=None):
    ap = argparse.ArgumentParser(prog="git-prune-merged", description=__doc__.split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--local", action="store_true", help="with --delete: also delete merged local branches")
    ap.add_argument("--base", help="merge base (default: origin/HEAD, else origin/main)")
    ap.add_argument("--keep", action="append", default=[], help="extra glob of branches to keep (repeatable)")
    ap.add_argument("--lang", choices=sorted(TEXT), default="en")
    a = ap.parse_args(argv)
    repo = Path(a.repo).expanduser().resolve()
    t = TEXT.get(a.lang, TEXT["en"])
    base = a.base or default_base(repo)
    try:
        remote, local, notes = plan(repo, base, a.keep, a.lang)
    except RuntimeError as e:
        print(t["fetch"].format(err=e)); return 1
    for n in notes:
        print(n)
    print(t["remote"].format(base=base, n=len(remote)) + ("".join(f"\n  {b}" for b in remote)))
    print(t["local"].format(how=t["local_del"] if a.local else t["local_list"], n=len(local)) + ("".join(f"\n  {b}" for b in local)))
    if not a.delete:
        print(t["dry"]); return 0
    if remote:
        out = subprocess.run(["git", "-C", str(repo), "push", base.split("/", 1)[0], "--delete", *remote], capture_output=True, text=True)
        print("\n".join("  " + l for l in (out.stdout + out.stderr).splitlines() if re.search(r"deleted|error|rejected", l)))
    nl = 0
    if a.local:
        for b in local:
            if subprocess.run(["git", "-C", str(repo), "branch", "-d", b], capture_output=True).returncode == 0:
                nl += 1
    git(repo, "fetch", "--prune", "-q", "origin", check=False)
    print(t["done"].format(r=len(remote), l=nl, repo=repo.name)); return 0


if __name__ == "__main__":
    sys.exit(main())
