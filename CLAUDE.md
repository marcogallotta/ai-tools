# ai-tools agent map

Read `README.md` for repository purpose and host integration. For every change under `dish/`, start at [`dish/docs/architecture/index.md`](dish/docs/architecture/index.md) and follow its task routing to the relevant ownership and invariant documents. Operational commands belong in runbooks; maintained architecture claims belong only in the architecture knowledge base.

## Dish safety and environments

- Genuine work uses production. Test is only for experiments, rehearsals, destructive testing, or Marco's explicit request. Confirm the target before an ambiguous mutation.
- Agents may use `dish-admin --profile test`; production administration is Marco-only.
- The production and test services are separate. Never print credentials, change the public Action route, or alter live dark-launch enablement without Marco's explicit authorization.
- Dark launch is evidence collection only. SQLite and Asana remain authoritative until an explicit, fenced cutover. Read-only status checks are permitted; operating procedure is in `dish/docs/database-backend-dark-launch-runbook.md`.
- A pasted live GPT transcript may refer to the deployed Action. Verify current state read-only before acting on an existing operation.

## Development and evidence

Create a repository-local environment with the current interpreter; do not assume an uploaded virtual environment is executable:

```sh
cd dish
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python scripts/dish-test-plan --base <revision>
```

If you are ChatGPT working from an uploaded or archived copy of this repo rather than the live filesystem: `dish/.venv/bin/python` is a symlink to the host Python that built it, not a self-contained interpreter, so on a relocated copy it can resolve to a different Python version than the one its packages were installed under. That makes a populated `.venv` misreport itself as empty — that's a version mismatch, not a missing dependency. Don't delete or rebuild it on that basis alone: compare `.venv`'s Python version (`pyvenv.cfg`) to the current `python3 --version` first. If they differ, rebuild a fresh `.venv` against the current interpreter and install normally. If normal installation then fails for lack of network/package-index access, report that honestly as a sandbox limitation — never report it as the repository lacking a dependency.

Use the test planner for the complete changed-path set and execute the union of focused tests and semantically required governed lanes. New in-scope paths must be classified in `dish/test_selection/ownership.csv`. Run the ordinary full suite before final delivery of a completed change block. Testing policy and evidence boundaries are in `dish/docs/testing.md` and `dish/docs/architecture/testing-boundaries.md`.

Do not package `.venv`, test caches, or generated test artifacts. Do not add runtime mutation paths, duplicate workflow authority in transports or CLIs, or preserve compatibility without a real producer or database-preservation requirement.

While doing assigned work, flag material maintainability or correctness issues you encounter that are relevant to the area you are touching: architecture/documentation gaps or contradictions that could cause mistakes; rules that do not match the actual code; likely bugs or weak/misleading tests; duplicated authority; significant code smells; brittle tooling; or recurring development friction. Do not broaden the implementation merely to fix unrelated observations. Report confirmed defects separately from suspicions and include enough concrete context for later triage.

## External instruction sources

`dish/deploy/gpt-action.md` contains a template, not the deployed custom GPT instructions. Changes to that template require a separate synchronized change in the live instructions repository and an explicit notice to Marco. If work changes the protocol's own structure, canonical fields, process records, or change classes, read `~/honest-pantry/dish-docs-design.md` and the relevant current Honest protocol/schema assets first.

## Memory

Do not create or update persistent memory files while working in this repository.
