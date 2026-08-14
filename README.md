# ai-tools

Marco's personal agent tooling. Claude and Codex sessions are given powerful, destructive
verbs — `git add`, Asana writes, `rm` — with no undo and no shared view of what another
session is doing. This repo replaces those raw verbs with narrow, guarded ones, and holds the
hook scripts and instruction files that make agents use them.

## The tools

**`tools/git-commit`** — commits instead of `git add` + `git commit`.

```
git-commit <file> [file...] -m "message"      # --help for flags
```

Stages and commits only the files you name, as one operation. A successful commit on `main` is
also pushed explicitly to `origin/main`; other branches and repositories without `origin` remain
commit-only. Refuses `.`, `-A`, `-u` — carpet staging collides with the index of whatever other
session is running. Also enforces the size limit on `dish-protocol.md`.

**`tools/asana`** — reads and writes Asana tasks and projects.

```
asana help                                    # operations and batch format
asana batch-apply <plan.json>                 # multi-step plans in one pass
```

Gets and sets notes, moves tasks between sections, creates tasks and subtasks, and adds comments.
Reads the API token from `~/.config/asana-cli/.env`. Every write goes through a hook prompt.

**`dish/`** — the `dish` protocol tool: the one validated path for writing protocol-governed
dish-task notes to Asana. Live multi-agent use runs through the laptop-hosted `dish-service`;
local direct mode is limited to controlled single-agent testing. See `dish/README.md`.

**`hooks/`** — host adapters and shared classifiers used by Claude Code and Codex
PreToolUse guards. They block carpet-bomb `git`/`rm` patterns, compound bash, raw redirects,
unguarded Asana and Anthropic-API writes, and branch changes in the shared primary checkout.

## Instructions for agents

- `AGENTS.md` — repo entry point; tells a session to read `CLAUDE.md` and this file first.
- `CLAUDE.md` — working rules for Dish documentation and the no-memory-writes policy.
- `CLAUDE-global.md` — cross-project preferences, loaded into every session everywhere. Lives
  here so it's version-controlled; Claude Code only auto-loads it under its real name,
  `~/.claude/CLAUDE.md`, hence the rename in the symlink.

## Symlinks

The repo is used through `~/.claude/` and `~/.local/bin/`, not from this directory:

| Repo path | Symlinked to |
|---|---|
| `CLAUDE-global.md` | `~/.claude/CLAUDE.md` |
| `hooks/` | `~/.claude/hooks/` |
| `codex/hooks.json` | `~/.codex/hooks.json` |
| `hooks/agent-reground` | `~/.local/bin/agent-reground` |
| `hooks/codex-protected-checkout` | `~/.local/bin/codex-protected-checkout` |
| `tools/git-commit` | `~/.local/bin/git-commit` |
| `tools/asana` | `~/.local/bin/asana` |
| `dish/dish` | `~/.local/bin/dish` |
| `dish/dish-admin` | `~/.local/bin/dish-admin` |
| `dish/dish-service` | `~/.local/bin/dish-service` |

Claude Code also loads the repository-owned `.claude/settings.json`, which wires the post-compaction re-grounding barrier for Dish worktrees. Agent-facing hook configs live under each host's discovery path. Shared executables live in
`~/.local/bin/`, which is on the real `PATH`, so `git-commit`, `asana`, `dish`, and
`dish-admin` work in Claude Code, Codex, and plain shell sessions alike. See
`codex/README.md` for the protected-checkout boundary and exact-head runtime certification.
