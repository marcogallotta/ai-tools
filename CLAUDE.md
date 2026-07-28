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
- `dish/docs/rollout.md` — separately authorized test-project rehearsal,
  migration, production cutover, and rollback;
- `dish/docs/future.md` — only work not already implemented.

Older design and implementation plans were removed; use Git history when their exact text is
needed.

## Dish agent environment

Every agent working under `dish/` must create its own repository-local environment; do not
assume an uploaded or host-global environment is runnable on the current Python interpreter:

```sh
cd ai-tools/dish
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest --fast
```

Use `pytest --fast` while iterating. Before handing back code or staged archives, run the complete
`.venv/bin/python -m pytest` suite, including the two tests skipped by `--fast`. Never package
`.venv` in a patch or archive.

## Live Dish smoke-test credentials

For an authorized live Dish smoke test run from the service host, load
`/home/marco/.config/dish-service/service.env` and map
`DISH_SERVICE_AGENT_TOKEN` to the client variable `DISH_SERVICE_TOKEN` and
`DISH_SERVICE_ADMIN_TOKEN` to `DISH_ADMIN_TOKEN`. `DISH_SERVICE_URL` is configured
through the interactive `~/.bashrc` path, so invoke the smoke-test shell interactively.
Never print, log, or include any token value in a transcript or report.

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
