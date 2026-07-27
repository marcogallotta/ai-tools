# ai-tools

This repo holds Marco's personal agent tooling. Read `README.md` next — it covers what the
repo is for, how `tools/git-commit` and `tools/asana` are invoked, and how the repo is wired
into `~/.claude/`.

## Working rules for Dish documentation

For changes under `dish/`, read `dish/docs/architecture.md` first. The current
documentation roles are:

- `dish/README.md` — installation, deployment, and operator entry points;
- `dish/docs/architecture.md` — current code structure, authority boundaries,
  invariants, persistence, recovery, and extension rules;
- `dish/docs/runtime-contract.md` — response, exit-status, retry, and
  troubleshooting contract;
- `dish/docs/dish-tool-future.md` — only work not already implemented.

`dish-tool-update.md` and `dish-tool-update-imp.md` are historical change records.
They may explain why a decision was made, but they do not override the current architecture,
runtime contract, code, or Honest protocol/schema assets. Older design and implementation
plans were removed; use Git history when their exact text is needed.

When architecture changes, update `architecture.md` in the same commit. Do not add
executable legacy mutation paths, duplicate workflow authority in transports or CLIs, or
preserve a state solely because a test can construct it. A compatibility path needs a real
producer or a real database-preservation requirement.

If work changes the protocol's own structure rather than only the tool, read
`~/honest-pantry/dish-docs-design.md` first. If it changes canonical fields, process-record
structure, or change classes, also read the relevant current Honest protocol and schema assets.

## Memory

No memory writing ever. Do not save, create, or update entries in the persistent memory
system (`~/.claude/projects/*/memory/`, `MEMORY.md`, etc.) while working in this repo, even
if the memory instructions elsewhere say to.
