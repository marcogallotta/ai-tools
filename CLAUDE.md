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
- `dish/docs/testing.md` — authoritative test gates, flaky-test diagnosis, quarantine, and
  test-artifact handling;
- `dish/docs/known-issues.md` — post-rollout candidates, testing boundaries, and accepted
  launch limitations;
- `dish/docs/runtime-contract.md` — response, exit-status, retry, and
  troubleshooting contract;
- `dish/docs/future.md` — broader future proposals not tracked as known issues.

Older design and implementation plans were removed; use Git history when their exact text is
needed.

Marco regularly tests the live GPT Action outside this session. When a pasted transcript
references an existing Cooking task/submission ID or an already-open operation, assume it came
from the deployed GPT Action, not this repo's local `dish`/`dish-admin` CLI, unless Marco says
otherwise. Verify current state read-only (`dish read`/`dish inspect`) before acting on it. Agents
may use `dish-admin --profile test` against the scratch environment. Production administration is
Marco-only: agents must never run `dish-admin` with the production profile.

Use production for genuine Dish work. Use test only for experiments, rehearsals, destructive
testing, or when Marco explicitly requests test. Never substitute test for production merely
because it feels safer. Before any mutation where the intended environment is ambiguous, stop and
confirm the target environment with Marco.

`dish/deploy/gpt-action.md`'s "Instructions for the GPT" section is a template only. The custom
GPT actually runs on `~/honest-pantry/dish-custom-gpt-instructions.md`, outside this
repo. Any edit to that template must be merged into the live file, in the same pass, as its own
commit in that repo. A repo commit does not update the running GPT: also tell Marco explicitly to
paste the live file's current contents into the custom GPT's instructions field.

## Dish agent environment

Every agent working under `dish/` must create its own repository-local environment; do not
assume an uploaded or host-global environment is runnable on the current Python interpreter:

```sh
cd ai-tools/dish
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest --smoke
.venv/bin/python -m pytest --database-boundary
```

Flaky-test detection uses a separate environment created from `requirements-flake.txt`; follow
`dish/docs/testing.md`. Normal smoke, database-boundary, and full-suite gates never rerun failures.

Use `pytest --smoke` for rapid confidence while iterating. The smoke gate is selected by explicit
per-test markers and enforces representative coverage of the launch-critical invariants. Run
`pytest --database-boundary` before handoff to exercise real empty-database bootstrap, historical
schema migration, SQLite concurrency, and backup/restore with production synchronization pragmas.
Before handing back code or staged archives, also run the complete `.venv/bin/python -m pytest`
suite. Never package `.venv` in a patch or archive.

## Live Dish smoke-test credentials

The service host keeps test and production running as separate systemd units. Test owns private and
Action ports `8765/8766`, production owns `8775/8776`, and the loopback Caddy router on `8786`
selects the public Action upstream. Environment files are
`/home/marco/.config/dish-service/test.env` and `prod.env`; databases and backups remain under the
matching `/home/marco/.local/state/dish/{test,prod}/` directory. Agent shells default to production;
use `dish --profile test` for the test environment. Production private access uses `:8445`. Inspect
the running public selection with `dish-action-route status`. Production is live; never change the
public route without Marco's explicit authorization.

Interactive `~/.bashrc` loads the test and production service and admin tokens from the two
owner-only service environment files. Claude and Codex inherit those tokens, but the production
admin prohibition above still applies; their settings pin only non-secret URLs and the production
default. Never print, log, or include any token value in a transcript or report.

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
