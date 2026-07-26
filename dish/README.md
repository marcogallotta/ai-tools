# dish

The one guarded path for writing protocol-governed dish-task notes to Asana. Agents don't edit
these notes directly — they go through `dish`, which validates the note's structure against the
protocol, checks the task hasn't changed underneath them, issues a single-use write token, and
routes work between author and verifier so nobody approves their own submission.

## Setup

`dish` and `dish-admin` re-exec themselves under `.venv/bin/python`, so the virtualenv has to
exist — they refuse to run without it rather than falling back to system python:

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`DISH_HONEST_PATH` must be set, pointing at the `honest` checkout that carries `DISH_VERSION`
and the machine schema. There is no default — unset, `dish` refuses to start; pointed at a
checkout without `DISH_VERSION`, it fails closed rather than loading protocol text unversioned.

Also read from the environment: `ASANA_PAT` (or `ASANA_ENV`, defaulting to
`~/.config/asana-cli/.env`) for the Asana token, and `DISH_DB_PATH` to override the state
database.

## The flow

Everything is keyed on a **submission id**. A unit of work moves through:

```
start      # open a submission against a task — declares kind and, for changes, level + reason
prepare    # hand it a note file; validated, staged, not yet written
submit     # spend the write token, write to Asana
approve    # verifier signs off      (or)  reject  — with a route for what happens next
```

Plus the read-only ones: `create` a task, list `sections`, `read` a task, `inspect` a
submission. Every command takes `--agent claude|gpt|codex` — that identity is what the
independence checks are built on.

```
./dish start <task_gid> --agent claude --kind change --change-level small --change-reason "..."
./dish prepare <submission_id> --agent claude --file note.md
./dish submit <submission_id>
```

## dish-admin

Break-glass, for when a submission is wedged rather than progressing: `recover` a submission
whose write outcome is unknown, `discard` or `unblock` one, `reopen` a settled question,
`migrate` an existing task into the protocol, `authorize-governed-change` for a field the
normal path won't let you touch, and `supply-evidence` / `record-human-decision` to satisfy a
hold. All of them demand a `--reason`.

## Tests

```
.venv/bin/pytest
```

## Design docs

- `docs/dish-tool.md` — the design. **Scoped to v1 only**; anything not needed for v1 to exist
  and work is deliberately not here.
- `docs/dish-tool-future.md` — everything that is *not* v1: the v1b enforcement flip, v2
  candidates, and ideas rejected outright.
- `docs/dish-tool-imp.md` — the staged build plan (v1a): rollout steps, module layout, open
  implementation questions.
- `docs/dish-tool-update.md` / `docs/dish-tool-update-imp.md` — compatibility analysis and
  revised plan, aligning the design with the frozen protocols in `~/honest-pantry-dish-rollout/`.
  Same design-doc/implementation-plan pairing as the two above.
- `docs/runtime-contract.md` — operator/activation notes.

ChatGPT-relay notes live in `~/honest-pantry-dish-rollout/dish-chatgpt-relay.md` (protocol repo,
not here — that's what's actually handed to ChatGPT).

Outside this repo: `~/honest-pantry/dish-docs-design.md` records which enforcement direction
Marco has approved (upstream of all of the above), and `~/honest-pantry/dish-protocol.md` is the
protocol itself — the thing `dish` validates against.

Authority runs one way — change plan → design doc → implementation plan — and the docs are
allowed to go stale against each other. See the repo root `CLAUDE.md` before editing any of
them.
