# ai-tools

This repo holds Marco's personal agent tooling: small CLIs that Claude and Codex
sessions invoke directly, plus design docs for tools in progress.

## Files

- `CLAUDE-global.md` — Marco's global CLAUDE.md, symlinked into `~/.claude/CLAUDE-global.md`
  and loaded into every Claude Code session regardless of working directory. It holds
  cross-project preferences and instructions, not anything specific to this repo.
- `bin/git-commit` — the git commit tool agents use instead of raw `git add`/`git commit`.
  It enforces stage-and-commit atomically with explicit file paths (no `.`/`-A`/`-u` carpet-bombing,
  to avoid index collisions between concurrent sessions), and carries a size-limit policy check
  specific to `dish-task-contract.md`. Symlinked into `~/.claude/bin/git-commit`.
- `bin/asana` — the CLI agents use to read and write Asana tasks/projects (get/set notes,
  move tasks between sections, batch-apply multi-step plans, etc). Reads the API token from
  `~/.config/asana-cli/.env`. Symlinked into `~/.claude/bin/asana`.
- `bin/dish-task-contract-tool.md`, `bin/dish-task-contract-tool-implementation-plan.md` —
  design docs for an in-progress tool; not yet implemented as a CLI.
- `bin/tests/` — pytest suite for `bin/asana` (run with `pytest` from `bin/`).

Both `bin/git-commit` and `bin/asana` are used by both Claude Code and Codex sessions, via
the symlinks under `~/.claude/bin/`.
