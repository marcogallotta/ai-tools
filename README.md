# ai-tools

This repo holds Marco's personal agent tooling: small CLIs that Claude and Codex
sessions invoke directly, plus the `dish` protocol tool and its design docs.

## Layout

- `CLAUDE-global.md` — Marco's global CLAUDE.md, symlinked into `~/.claude/CLAUDE.md`
  (the filename Claude Code actually auto-loads for every session regardless of working
  directory — it must be named `CLAUDE.md`, not `CLAUDE-global.md`, in `~/.claude/`). Holds
  cross-project preferences and instructions, not anything specific to this repo.
- `tools/git-commit` — the git commit tool agents use instead of raw `git add`/`git commit`.
  Enforces stage-and-commit atomically with explicit file paths (no `.`/`-A`/`-u` carpet-bombing,
  to avoid index collisions between concurrent sessions), and carries a size-limit policy check
  specific to `dish-protocol.md`. Symlinked into `~/.claude/bin/git-commit`.
- `tools/asana` — the CLI agents use to read and write Asana tasks/projects (get/set notes,
  move tasks between sections, batch-apply multi-step plans, etc). Reads the API token from
  `~/.config/asana-cli/.env`. Symlinked into `~/.claude/bin/asana`.
- `dish/` — the `dish` protocol tool: entry scripts (`dish`, `dish-admin`), the `dish_tool`
  package, its pytest suite (`dish/tests/`, run with `pytest` from `dish/`), design docs
  (`dish/docs/`), and runtime state (`dish/var/`).
- `hooks/` — Claude Code hook scripts (PreToolUse guards and nudges: blocking carpet-bomb
  git/rm patterns, compound bash, raw redirects, unguarded Asana/Anthropic-API writes, etc).
  Symlinked as a directory into `~/.claude/hooks/`.

`tools/git-commit` and `tools/asana` are used by both Claude Code and Codex sessions, via
the symlinks under `~/.claude/bin/`.

## dish design docs

- `dish/docs/dish-tool.md` — design draft for `dish`, the one guarded, validated path for
  writing protocol-governed dish-task notes to Asana (structural validation, single-use write
  tokens, staleness checks, verifier routing). **Scoped to v1 only** — everything not needed
  for v1 to exist and work lives in `dish-tool-future.md` instead.
- `dish/docs/dish-tool-future.md` — everything about the same tool that is *not* v1: the v1b
  enforcement flip, v2 candidate features, and ideas considered and rejected outright.
- `dish/docs/dish-tool-imp.md` — the staged build plan (v1a): rollout steps, file/module
  layout, open implementation questions.
- `dish/docs/dish-tool-update.md` / `dish/docs/dish-tool-update-imp.md` — the compatibility
  analysis and revised implementation plan bringing the tool design in line with the frozen
  protocols in `~/honest-pantry-dish-rollout/`. Same design-doc/implementation-plan relationship
  as the pair above.
- `dish/docs/dish-tool-activation.md`, `dish/docs/dish-chatgpt-relay.md` — operator/activation
  and ChatGPT-relay notes for the same tool.
- Related files, not in this repo:
  - `~/honest-pantry/dish-docs-design.md` — the tracker of what enforcement direction Marco
    has approved for the tool, upstream of the two docs above.
  - `~/honest-pantry/dish-protocol.md` — the actual protocol the tool validates dish-task
    notes against (canonical task structure, change classes, verification rules).

See `CLAUDE.md` for the working rules that govern these docs (staleness tolerance, authority
order between the change plan/design doc/implementation plan) and for memory-writing policy.
