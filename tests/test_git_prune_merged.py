import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from atlat_tools import git_prune_merged  # noqa: E402


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.remote), cwd=base)
        self.repo = base / "repo"
        sh("git", "clone", "-q", str(self.remote), str(self.repo), cwd=base)
        env_cmds = [("git", "config", "user.email", "t@example.com"), ("git", "config", "user.name", "t")]
        for c in env_cmds:
            sh(*c, cwd=self.repo)
        (self.repo / "a.txt").write_text("a")
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "init", cwd=self.repo)
        sh("git", "push", "-q", "-u", "origin", "main", cwd=self.repo)
        # merged feature branch, pushed
        sh("git", "checkout", "-q", "-b", "feat/done", cwd=self.repo)
        (self.repo / "b.txt").write_text("b")
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "feat", cwd=self.repo)
        sh("git", "push", "-q", "-u", "origin", "feat/done", cwd=self.repo)
        sh("git", "checkout", "-q", "main", cwd=self.repo)
        sh("git", "merge", "-q", "feat/done", cwd=self.repo)
        sh("git", "push", "-q", "origin", "main", cwd=self.repo)
        # unmerged branch, pushed
        sh("git", "checkout", "-q", "-b", "feat/open", cwd=self.repo)
        (self.repo / "c.txt").write_text("c")
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-q", "-m", "open", cwd=self.repo)
        sh("git", "push", "-q", "-u", "origin", "feat/open", cwd=self.repo)
        sh("git", "checkout", "-q", "main", cwd=self.repo)
        # merged branch kept by glob, pushed
        sh("git", "branch", "release-1", "main", cwd=self.repo)
        sh("git", "push", "-q", "origin", "release-1", cwd=self.repo)
        sh("git", "branch", "hotfix/keep", "main", cwd=self.repo)
        sh("git", "push", "-q", "origin", "hotfix/keep", cwd=self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_lists_only_merged_unprotected(self):
        remote, local, notes = git_prune_merged.plan(self.repo, "origin/main", keep_globs=["hotfix/*"])
        self.assertEqual(remote, ["feat/done"])
        self.assertEqual(local, ["feat/done"])  # main is protected, feat/open unmerged, hotfix kept, release kept

    def test_delete_remote_only_by_default(self):
        with redirect_stdout(io.StringIO()):
            rc = git_prune_merged.main([str(self.repo), "--delete", "--keep", "hotfix/*"])
        self.assertEqual(rc, 0)
        remotes = sh("git", "branch", "-r", cwd=self.repo)
        self.assertNotIn("origin/feat/done", remotes)
        self.assertIn("origin/feat/open", remotes)
        self.assertIn("origin/hotfix/keep", remotes)
        self.assertIn("feat/done", sh("git", "branch", cwd=self.repo))  # local kept without --local

    def test_delete_local_with_flag(self):
        with redirect_stdout(io.StringIO()):
            git_prune_merged.main([str(self.repo), "--delete", "--local", "--keep", "hotfix/*"])
        self.assertNotIn("feat/done", sh("git", "branch", cwd=self.repo))

    def test_checked_out_in_worktree_is_kept(self):
        wt = Path(self.tmp.name) / "wt"
        sh("git", "worktree", "add", "-q", str(wt), "feat/done", cwd=self.repo)
        remote, local, notes = git_prune_merged.plan(self.repo, "origin/main", keep_globs=["hotfix/*"])
        self.assertEqual(remote, [])
        self.assertTrue(any("worktree" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
