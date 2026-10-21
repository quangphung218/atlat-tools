# atlat-tools

Small, dependency-free command-line tools for people who run coding agents (Claude Code, Codex, Cursor) on their own
machine. Each tool is one Python file, works on its own, needs nothing but Python 3.10+, never calls an LLM and never
sends anything off the machine. They came out of running an "agent manager" day to day; every check is a fact you can
verify, not a judgement call.

| Tool | One line |
|---|---|
| `agent-lint` | Deterministic linter for agent configuration: dead `@imports` and paths in CLAUDE.md/AGENTS.md, risky `settings.json` permissions, hooks that call missing scripts, broken skills, MCP commands that are not installed, leaked secrets. Exit 1 on errors, so it works as a CI gate. |
| `claude-observe` | What you actually do with Claude Code over the last N days: prompts per project, requests you keep repeating, which skills and tools get used, where tools fail. Reads `~/.claude/history.jsonl` and the transcripts; keeps 80 characters per prompt and masks anything that looks like a key. |
| `claude-skills` | Manage the global skill folder with real usage numbers: list, unused, upstream drift of `npx skills` installs, frontmatter patching, enable/disable through `skillOverrides` only, upgrades with backups, data-driven proposals. |
| `recall` | Long-term memory for a working folder: SQLite FTS5 search over your markdown notes and your Claude Code conversations, newest first, diacritics-insensitive. |
| `logexpect` | "The feature I just shipped must actually run": register an expectation on a log file, learn when the first real occurrence appears and whether it ended well (ok/bad regex, correlated by task/project id), get told when it never happens. |
| `agent-health` | Health of a machine that runs coding agents: load with the top process and which agent session owns it, RAM/swap, API reachability, agent sessions with idle time, dev servers and headless browsers left behind, disk hogs. `--fix` kills only provably orphaned leftovers. |
| `vn-style` | Linter for a Vietnamese "house voice" (replies, reports, chat): forbidden pronouns, filler openers/closers, padding words, hedges, vague time estimates, spaces before punctuation, old-style tone marks, Title Case. `--fix` applies the mechanical ones. |
| `git-prune-merged` | Delete remote (and optionally local) branches merged into main, never touching protected branches, branches with open PRs or branches checked out in a worktree. Lists first, deletes with `--delete`. |

Every tool has `--help`, `--json` where it makes sense, and `--lang vi` for Vietnamese output.

## Install

```bash
uv tool install git+https://github.com/quangphung218/atlat-tools      # or: pipx install git+https://github.com/quangphung218/atlat-tools
```

No installer at hand? Each file runs directly:

```bash
curl -fsSL https://raw.githubusercontent.com/quangphung218/atlat-tools/main/atlat_tools/agent_lint.py -o agent-lint && python3 agent-lint --global
```

## agent-lint

```bash
agent-lint                      # current directory
agent-lint ~/code/app ~/code/api --global     # several roots plus ~/.claude
agent-lint --json | jq '.findings[] | select(.level=="error")'
agent-lint --out review/agent-lint.md --lang vi
```

What it reports:

- `CLAUDE.md` / `AGENTS.md` (root and two folders deep): over 300 lines (`--max-lines`), `@imports` to missing files,
  backtick paths that do not exist. Files under a `template` folder are only noted.
- `.claude/settings.json`, `settings.local.json`: invalid JSON, `defaultMode = bypassPermissions`, `allow` lists that are
  too broad or let `rm -rf`, `sudo`, `git push` or arbitrary interpreters run without a prompt, `deny` rules written as
  `**/…` (they only match inside the working directory), hooks whose script does not exist.
- `.claude/skills`: folders without `SKILL.md`, skills nested under a group folder (never loaded), frontmatter without
  `name`/`description`, project skills that shadow a global one, `.agents/skills` present but not loaded.
- `.mcp.json`, `.cursor/mcp.json`, `.codex/config.toml`: servers whose command is not installed here.
- Anything under `.claude/`, `.codex/`, `.cursor/` plus the instruction files: strings that look like API keys, tokens or
  private keys (shown masked).

## claude-observe

```bash
claude-observe                         # last 14 days, markdown
claude-observe --days 30 --json
claude-observe --exclude-prefix "You are the builder agent" --exclude-project "bot/worktrees"
claude-observe --out ~/notes/claude-usage   # writes <date>.md and <date>.json
```

Use `--exclude-prefix` for prompts that another agent typed into your sessions, and `--exclude-project` for sessions a
bot runs in its own worktrees, so the numbers describe you. A typical result on a busy machine: 800 MB of transcripts,
2–3 seconds, and the discovery that 7 of 34 enabled skills were ever invoked.

## claude-skills

```bash
claude-skills list                     # name · source · enabled · uses in 14 days · last edit · lines · lint
claude-skills unused --days 30
claude-skills check                    # upstream drift (needs `gh`) + lint
claude-skills propose                  # what the numbers suggest
claude-skills propose --apply          # does the deterministic part: patch frontmatter, upgrade drifted skills you use
claude-skills propose --apply --disable-unused   # also switch off enabled skills with zero uses
claude-skills disable research handoff --why "never used"
claude-skills enable research
```

Rules it follows: only `skillOverrides` in `settings.json` is ever edited (everything else in the file is preserved
byte-for-byte in a backup under `~/.claude/backups/skills/<stamp>/`); every change is appended to `~/.claude/skills.log`;
`upgrade` runs `npx skills update` after backing up the skill folder and keeps the old copy if the new `SKILL.md` lost
its frontmatter. Usage comes from `claude-observe` (run automatically, or pass `--usage file.json`).

## recall

```bash
recall "deploy failed"                 # refreshes the index, prints the 10 newest hits across notes and chats
recall "trien khai" -n 20              # matches "triển khai" too
recall "x" --source chat               # chat | notes | memory
recall --index --quiet                 # from a SessionStart hook
recall --stats
```

By default it indexes the current folder's markdown/text files (depth 4, skipping `node_modules` and friends), the
Claude Code transcripts of this project (your messages and the assistant's replies only) and the project's auto-memory.
Set defaults in `~/.config/recall/config.json`: `{"dirs": ["~/notes"], "all_projects": true, "lang": "vi"}`.

## logexpect

```bash
logexpect add "PR #62 rerun" '"event":"project_item_rerun"' --log ~/app/app.log --ok '"event":"rerun_done"' --bad '"event":"rerun_failed"'
logexpect check --log ~/app/app.log        # from a cron job or a shell hook
logexpect wait --log ~/app/app.log --notify-cmd 'say {text}'
logexpect list; logexpect done 2; logexpect prune
```

The first line matching the main regex since `add` is the hit; lines in the next two hours that carry the same
`"task"`/`"project"`/`"id"` (or your `--key`) decide ok/bad. Pending for seven days is reported once as stale: wrong regex or
unused feature. JSON lines (ts/time/timestamp) and text lines with a leading ISO timestamp both work.

## agent-health

```bash
agent-health                 # markdown report: verdict, load, sessions, stray processes
agent-health --brief         # one line for a status bar or a daily note
agent-health --disk          # add cache and node_modules sizes
agent-health --fix           # kill orphaned dev servers / headless browsers older than 2 h
agent-health --agent aider --idle-hours 24
```

Sessions are processes named `claude`, `codex`, `gemini`, `cursor-agent`, `aider`, `opencode`, `goose` (add more with
`--agent`). When load exceeds the core count the report names the hottest process and the agent session it belongs to.
Exit code is 1 when there is a warning, so it doubles as a check.

## vn-style

```bash
vn-style reply.md                    # findings with line numbers
printf '%s' "$MESSAGE" | vn-style -  # lint a message before sending it
vn-style docs/ --strict              # warnings fail too
vn-style reply.md --fix              # NFC, no space before punctuation, hòa → hoà
vn-style x.md --rules my-voice.json  # your own pronoun / filler / hedge lists
```

Defaults encode a terse, conclusion-first voice: no `bạn`/`tôi`/`mình` (error), no "Dưới đây là…" openers or "Hy vọng…"
closers, no `tiến hành`/`một cách` padding, numbers instead of "hơi lâu", new-style tone marks, no Title Case. Fixes never
change wording. For product copy, UI strings and i18n files use a dedicated validator such as the one that ships with the
`vietnamese-tech-writing` skill; this tool is about voice.

## git-prune-merged

```bash
git-prune-merged                         # list
git-prune-merged ~/code/app --delete     # delete merged remote branches
git-prune-merged --delete --local --keep 'hotfix/*'
```

## Design notes

- No dependencies, no daemon, no network (except `claude-skills check`, which asks GitHub for tree hashes through `gh`,
  `upgrade`, which runs `npx skills update`, `git-prune-merged`, which talks to your git remote and `gh`, and `agent-health`,
  which probes one URL).
- Everything that changes files makes a backup first and writes one log line. Nothing deletes user files.
- Output is markdown you can paste into a report, or JSON you can pipe into the next tool.

## Tiếng Việt

Bộ công cụ nhỏ cho người chạy agent code trên máy mình: `agent-lint` soát cấu hình agent (đường dẫn chết, quyền quá rộng,
hook gọi script không có, skill hỏng, lộ secret), `claude-observe` cho biết anh thật sự dùng Claude Code vào việc gì và
skill nào được gọi, `claude-skills` quản kho skill bằng số liệu đó (tắt cái không dùng, nâng cấp cái lệch upstream, chỉ
đụng `skillOverrides`), `recall` tìm lại trong ghi chú và lịch sử trò chuyện, bỏ dấu khi so; `logexpect` chờ lần chạy thật đầu tiên của tính
năng vừa deploy trong log; `agent-health` sức khoẻ máy chạy agent; `vn-style` soát văn tiếng Việt theo giọng nhà (em/anh,
không câu đệm, dấu kiểu mới) có `--fix`; `git-prune-merged` dọn branch đã merge. Thêm `--lang vi` để nhận thông báo tiếng Việt.

## License

MIT
