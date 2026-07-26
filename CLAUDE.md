# ai-tools

This repo holds Marco's personal agent tooling. See `README.md` for the file/directory
layout and what each design doc covers.

## Working rules for the dish design docs

`dish/docs/dish-tool.md`, `dish-tool-future.md`, and `dish-tool-imp.md` are allowed to go
stale relative to each other — that's fine, not a bug. When iterating/designing on one doc,
just work on that doc. Don't feel obliged to keep the others in sync in the same pass, and
especially don't hold back on the future doc — it's deliberately a loose catch-all net. Going
stale there is expected and cheap to reconcile later, e.g. by diffing one doc's git history
against a known baseline commit to see what changed and what it implies for the others.

The one case that's different: when a change *intentionally moves or resolves content between
two of these docs* — e.g. resolving an open question in the implementation plan by relocating
it into the future doc's v2 list — that's a single logical edit that happens to span two files,
not two unrelated edits sharing a commit. Commit it as one commit, both files together.

**Authority flows one way: change plan → design doc (`dish-tool.md`) → implementation plan.
It is not a three-way sync.** Once the design doc has made a concrete decision, the design doc
wins outright, even where the change plan's wording is older, vaguer, or broader — that is
expected and not a discrepancy to resolve. The change plan is never updated to tighten it back
up. The *only* pairwise sync obligation is design doc ↔ implementation plan, since the plan's
job is to build exactly what the design doc specifies. When the implementation plan flags
something as an open question, first check whether the design doc has actually already decided
it — if so, it is not open; cite the resolution and move on, rather than re-litigating it as a
live choice.

If you are working *on* the two design docs above (drafting, revising, reconciling design
decisions) — as opposed to using them as a spec to build the tool — also read
`~/honest-pantry/dish-docs-design.md` first, since it's the source of truth for what's approved.
If that work goes deep enough into the protocol's own structure (not just the tool that edits
it — e.g. its canonical manifest, process-record fields, change-class definitions), also read
`~/honest-pantry/dish-protocol.md`.

## Memory

No memory writing ever. Do not save, create, or update entries in the persistent memory
system (`~/.claude/projects/*/memory/`, `MEMORY.md`, etc.) while working in this repo, even
if the memory instructions elsewhere say to.
