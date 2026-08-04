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
.venv/bin/python scripts/dish-test-plan --base <revision>
```

ChatGPT environments may use a different Python minor version from the uploaded repository.
Do not assume the uploaded `dish/.venv` interpreter is executable. Create a fresh local virtual
environment with the available Python interpreter and install `requirements-test.txt`. If a required
dependency is unavailable from that requirements installation, the uploaded `.venv` site-packages
may be used only as a documented fallback after confirming they are compatible with the current
interpreter.

Flaky-test detection uses a separate environment created from `requirements-flake.txt`; follow
`dish/docs/testing.md`. Authoritative first-attempt lanes never rerun failures automatically.

For Dish code or test changes, run `scripts/dish-test-plan` with the complete changed-path set and
execute its focused tests and governed lanes. Treat `test_selection/ownership.csv` as a strong
current-HEAD prior, not a ceiling: assess the actual invariant, authority, durable state, external
effect, transaction boundary, and release consequence changed; add any semantically required lane;
and take the union across mixed changes. New in-scope paths must be classified in the same change.
Escalate to Marco only when the owning architecture or acceptable evidence remains materially
ambiguous.

The ordinary full suite is required before merge or integration of a completed change block, before
a final staged archive, after conflict resolution affecting shared code, after global selector,
fixture, dependency, marker, or runner-policy changes, and before release or cutover certification.
It is not mandatory after every scoped edit. Keep smoke, SQLite database-boundary, PGlite, native
PostgreSQL, migration, mutation, acceptance, and ordinary pytest evidence separately reported as
defined in `dish/docs/testing.md`. Never package `.venv` in a patch or archive.

## Live Dish rehearsal credentials

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

## Dark launch (dish_pg shadow capture)

Dark launch mirrors legacy command completions into PostgreSQL via a shadow envelope so real
data accumulates for validation; it does not make PostgreSQL authoritative or SQLite/Asana any
less so. Treat a live dark-launch instance as read-only evidence, never as production authority,
until the separate, explicit activation event described in `dish/docs/architecture.md`.

Agents may run `scripts/dish-pg-dark-launch status` (read-only) to check backlog, lag, and
mismatch counts. Never flip `DISH_DARK_LAUNCH_MODE`, install/start/stop
`dish-shadow-worker.service`, or touch the kill switch without Marco's explicit authorization —
each changes live legacy-service configuration. Operating steps live in
`dish/docs/database-backend-dark-launch-runbook.md`.

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
