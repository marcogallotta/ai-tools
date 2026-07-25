# ai-tools

This repo holds Marco's personal agent tooling: small CLIs that Claude and Codex
sessions invoke directly, plus design docs for tools in progress.

## Files

- `CLAUDE-global.md` — Marco's global CLAUDE.md, symlinked into `~/.claude/CLAUDE.md`
  (the filename Claude Code actually auto-loads for every session regardless of working
  directory — it must be named `CLAUDE.md`, not `CLAUDE-global.md`, in `~/.claude/`). It
  holds cross-project preferences and instructions, not anything specific to this repo.
- `bin/git-commit` — the git commit tool agents use instead of raw `git add`/`git commit`.
  It enforces stage-and-commit atomically with explicit file paths (no `.`/`-A`/`-u` carpet-bombing,
  to avoid index collisions between concurrent sessions), and carries a size-limit policy check
  specific to `dish-protocol.md`. Symlinked into `~/.claude/bin/git-commit`.
- `bin/asana` — the CLI agents use to read and write Asana tasks/projects (get/set notes,
  move tasks between sections, batch-apply multi-step plans, etc). Reads the API token from
  `~/.config/asana-cli/.env`. Symlinked into `~/.claude/bin/asana`.
- `bin/docs/dish-tool.md` — design draft for an in-progress tool named `dish` that will be the one
  guarded, validated path for writing protocol-governed dish-task notes to
  Asana (structural validation, single-use write tokens, staleness checks, verifier routing). Not
  yet implemented as a CLI. **Scoped to v1 only** — everything not needed for v1 to exist and
  work lives in `dish-tool-future.md` instead (see below).
- `bin/docs/dish-tool-future.md` — everything about the same tool that is *not* v1: the
  v1b enforcement flip, v2 candidate features, and ideas considered and rejected outright. Split
  out of the design doc above so that doc can stay focused on exactly what v1 needs.
- `bin/docs/dish-tool-imp.md` — the staged build plan (v1a) for that
  tool: rollout steps, file/module layout, open implementation questions.
- `bin/docs/dish-tool-update.md` / `bin/docs/dish-tool-update-imp.md` — the compatibility-analysis
  and revised implementation plan bringing the tool design in line with the frozen protocols in
  `~/honest-pantry-dish-rollout/`. Same design-doc/implementation-plan relationship as the pair
  above.
- `bin/docs/dish-tool-activation.md`, `bin/docs/dish-chatgpt-relay.md` — operator/activation and
  ChatGPT-relay notes for the same tool.

  **These three docs are allowed to go stale relative to each other — that's fine, not a bug.**
  When actually iterating/designing on one doc (e.g. adding to the v1 design doc), just work on
  that doc. Don't feel obliged to keep the other two in sync in the same pass, and especially
  don't hold back on the future doc — it's deliberately a loose catch-all net, not a doc that
  needs to track the others in real time. Going stale there is expected and cheap to reconcile
  later, e.g. by diffing one doc's git history against a known baseline commit to see what
  changed and what it implies for the others.

  The one case that's different: when a change *intentionally moves or resolves content between
  two of these docs* — e.g. resolving an open question in the implementation plan by relocating
  it into the future doc's v2 list — that's a single logical edit that happens to span two files,
  not two unrelated edits sharing a commit. Commit it as one commit, both files together.
- Related files, not in this repo:
  - `~/honest-pantry/dish-docs-design.md` — the tracker of what enforcement
    direction Marco has approved for that tool, upstream of the two docs above.
  - `~/honest-pantry/dish-protocol.md` — the actual protocol the tool validates dish-task
    notes against (canonical task structure, change classes, verification rules).

  **Authority flows one way: change plan → design doc (`dish-tool.md`) →
  implementation plan. It is not a three-way sync.** Once the design doc has made a concrete
  decision, the design doc wins outright, even where the change plan's wording is older, vaguer,
  or broader — that is expected and not a discrepancy to resolve. The change plan is never
  updated to tighten it back up. The *only* pairwise sync obligation is design doc ↔
  implementation plan, since the plan's job is to build exactly what the design doc specifies.
  When the implementation plan flags something as an open question, first check whether the
  design doc has actually already decided it — if so, it is not open; cite the resolution and
  move on, rather than re-litigating it as a live choice.

  If you are working *on* the two design docs above (drafting, revising, reconciling design
  decisions) — as opposed to using them as a spec to build the tool — also read
  `dish-docs-design.md` first, since it's the source of truth for what's approved.
  If that work goes deep enough into the protocol's own structure (not just the tool that edits
  it — e.g. its canonical manifest, process-record fields, change-class definitions), also read
  `dish-protocol.md`.
- `bin/tests/` — pytest suite for `bin/asana` (run with `pytest` from `bin/`).
- `hooks/` — Claude Code hook scripts (PreToolUse guards and nudges: blocking carpet-bomb
  git/rm patterns, compound bash, raw redirects, unguarded Asana/Anthropic-API writes, etc).
  Symlinked into `~/.claude/hooks/`.

Both `bin/git-commit` and `bin/asana` are used by both Claude Code and Codex sessions, via
the symlinks under `~/.claude/bin/`.

## Memory

No memory writing ever. Do not save, create, or update entries in the persistent memory
system (`~/.claude/projects/*/memory/`, `MEMORY.md`, etc.) while working in this repo, even
if the memory instructions elsewhere say to.
