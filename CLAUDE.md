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
- `dish/docs/known-issues.md` — post-rollout candidates, testing boundaries, and accepted
  launch limitations;
- `dish/docs/runtime-contract.md` — response, exit-status, retry, and
  troubleshooting contract;
- `dish/docs/rollout.md` — separately authorized test-project rehearsal,
  migration, production cutover, and rollback;
- `dish/docs/future.md` — broader future proposals not tracked as known issues.

Older design and implementation plans were removed; use Git history when their exact text is
needed.

Marco regularly tests the live GPT Action outside this session. When a pasted transcript
references an existing Cooking task/submission ID or an already-open operation, assume it came
from the deployed GPT Action, not this repo's local `dish`/`dish-admin` CLI, unless Marco says
otherwise. Verify current state read-only (`dish read`/`dish inspect`) before acting on it; never
run `dish-admin` write/recovery commands yourself — only Marco can, regardless of which surface
got stuck.

`dish/deploy/gpt-action.md`'s "Instructions for the GPT" section is a template only. The custom
GPT actually runs on `~/honest-pantry-dish-rollout/dish-custom-gpt-instructions.md`, outside this
repo. Any edit to that template must be merged into the live file, in the same pass, as its own
commit in that repo. A repo commit does not update the running GPT: also tell Marco explicitly to
paste the live file's current contents into the custom GPT's instructions field.

## Dish agent environment

Every agent working under `dish/` must create its own repository-local environment; do not
assume an uploaded or host-global environment is runnable on the current Python interpreter:

```sh
cd ai-tools/dish
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest --smoke
```

Use `pytest --smoke` for rapid confidence while iterating. Before handing back code or staged
archives, run the complete `.venv/bin/python -m pytest` suite. Never package `.venv` in a patch or
archive.

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
