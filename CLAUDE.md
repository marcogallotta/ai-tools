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
- `bin/dish-task-contract-tool.md` — design draft for an in-progress tool (name not yet decided)
  that will be the one guarded, validated path for writing contract-governed dish-task notes to
  Asana (structural validation, single-use write tokens, staleness checks, verifier routing). Not
  yet implemented as a CLI. **Scoped to v1 only** — everything not needed for v1 to exist and
  work lives in `dish-task-contract-tool-future.md` instead (see below).
- `bin/dish-task-contract-tool-future.md` — everything about the same tool that is *not* v1: the
  v1b enforcement flip, v2 candidate features, and ideas considered and rejected outright. Split
  out of the design doc above so that doc can stay focused on exactly what v1 needs.
- `bin/dish-task-contract-tool-implementation-plan.md` — the staged build plan (v1a) for that
  tool: rollout steps, file/module layout, open implementation questions.

  **Work on one of these three docs at a time.** When iterating on the v1 design doc, leave the
  implementation plan and the future doc untouched (and vice versa), and commit each meaningful
  edit rather than batching several uncommitted changes together. The docs are meant to be
  reconciled with each other later by diffing git history against a known baseline commit — e.g.
  "what changed in the v1 design doc since the split, and what does that imply for the
  implementation plan." That only works cleanly if the untouched doc's history has no unrelated
  edits mixed in during the same window. Git history gives you *what changed*; reconciling it
  into the other doc is still a judgment call for whoever picks that up next — it isn't
  automatic, but a clean commit trail is what makes that judgment call tractable.
- Related files, not in this repo:
  - `~/honest-pantry/dish-task-contract-change-plan.md` — the authoritative tracker of what
    enforcement direction Marco has actually approved for that tool vs. what's still pending
    design/decision. The two docs above are downstream of it and must stay consistent with it.
  - `~/honest-pantry/dish-task-contract.md` — the actual contract the tool validates dish-task
    notes against (canonical task structure, change classes, verification rules).

  If you are working *on* the two design docs above (drafting, revising, reconciling design
  decisions) — as opposed to using them as a spec to build the tool — also read
  `dish-task-contract-change-plan.md` first, since it's the source of truth for what's approved.
  If that work goes deep enough into the contract's own structure (not just the tool that edits
  it — e.g. its canonical manifest, process-record fields, change-class definitions), also read
  `dish-task-contract.md`.
- `bin/tests/` — pytest suite for `bin/asana` (run with `pytest` from `bin/`).
- `hooks/` — Claude Code hook scripts (PreToolUse guards and nudges: blocking carpet-bomb
  git/rm patterns, compound bash, raw redirects, unguarded Asana/Anthropic-API writes, etc).
  Symlinked into `~/.claude/hooks/`.

Both `bin/git-commit` and `bin/asana` are used by both Claude Code and Codex sessions, via
the symlinks under `~/.claude/bin/`.
