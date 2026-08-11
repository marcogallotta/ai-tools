# ai-tools agent map

Read `README.md` for repository purpose and host integration. For every change under `dish/`, start at [`dish/docs/architecture/index.md`](dish/docs/architecture/index.md) and follow its task routing to the relevant ownership and invariant documents. Operational commands belong in runbooks; maintained architecture claims belong only in the architecture knowledge base.

## Agent roles

For Dish work, role routing lives in [`dish/docs/agents/index.md`](dish/docs/agents/index.md).

If you are told to assume, act as, or hand work to a named Dish role, read that index first and then the mapped standing role contract before acting. Do not infer role policy from a nearby file or repeat stable role rules in task handoffs.

Standing role contracts contain stable policy so task handoffs can stay short and contain only the task-specific delta. If a handoff conflicts with a standing role contract, flag the conflict rather than silently choosing a new policy.

For patch application or commit/integration work, follow the patch-application verification rule in `dish/docs/agents/implementation.md`: determine the repository root first and verify the expected diff exists after application. A successful command exit alone is not evidence that a patch was applied.

## Dish safety and environments

- Genuine work uses production. Test is only for experiments, rehearsals, destructive testing, or Marco's explicit request. Confirm the target before an ambiguous mutation.
- Agents may use `dish-admin --profile test`; production administration is Marco-only.
- Do not run raw destructive SQL against production. A reviewed script must be written. Marco's explicit approval is required for genuine exceptions.
- The production and test services are separate. Never print credentials, change the public Action route, or alter live dark-launch enablement without Marco's explicit authorization.
- Dark launch is evidence collection only. SQLite and Asana remain authoritative until an explicit, fenced cutover. Read-only status checks are permitted; operating procedure is in `dish/docs/database-backend-dark-launch-runbook.md`.
- A pasted live GPT transcript may refer to the deployed Action. Verify current state read-only before acting on an existing operation.

## Scheduled reviews

- **Transition-records cleanup** (`dish/docs/postgresql-cutover.md` Addendum B #11): when working on Dish, if today's date is on or after 2026-11-08, tell Marco that this review is due. Marco must either perform/reassess the cleanup work or explicitly move the review date forward. Never delete transition records automatically or solely because the review date has passed.

## Development and evidence

Execution environment and repository transport are **agent-host-specific**. A Dish role does not imply a particular host or bootstrap path.

### Claude Code and Codex

Claude Code and Codex use their live checkout plus their host-native Git/tooling and environment. Create or use the repository-local environment with the current interpreter as needed:

```sh
cd dish
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python scripts/dish-test-plan --base <revision>
```

The ChatGPT GitHub connector and ChatGPT dependency-bundle retrieval path below are **not** standing instructions for Claude Code or Codex. Do not put connector setup, Actions-artifact bundle retrieval, or a user-supplied bundle into their handoffs unless Marco explicitly makes that a task-specific requirement.

### ChatGPT agents

For ChatGPT agents, use the connected GitHub integration as source/history authority for this private repository. Fetch current authoritative source from GitHub; never let a source snapshot inside a dependency artifact override GitHub source/history.

Bootstrap Python runtime dependencies from the authoritative GitHub-built dependency bundle rather than asking Marco to upload a virtual environment or bundle manually:

- the versioned GitHub Release is publication authority for the bundle;
- the matching GitHub Actions artifact is the ChatGPT retrieval/evidence mirror and must carry the same bundle identity;
- resolve the current expected bundle identity from authoritative repository metadata/tooling rather than hard-coding a historical bundle ID;
- download through the GitHub connector, verify the manifest and checksum, and use `scripts/dependency_bundle.py install` to recreate the repository environments;
- preserve the bundle's fail-closed Python/platform/architecture/libc checks. For glibc, a runtime newer than the declared minimum is compatible; an older runtime is not;
- if connector access, the matching artifact, or runtime compatibility is unavailable, report that exact missing capability rather than falling back to an uploaded archived `.venv`.

This connector-and-bundle bootstrap is **ChatGPT-only policy**. It must not be copied into Claude Code or Codex handoffs.

On Marco's local development machine, the preferred reviewed fast path is `.venv/bin/python scripts/dish-test-lane parallel-safe --workers 4`, or the planner's equivalent `--parallel-workers 4` focused command. Four workers is a locally benchmarked recommendation, not a repository-wide hardware constant. Keep serial execution available for diagnosis and for every selection outside the currently qualified parallel-safe inventory.

Use the test planner for the complete changed-path set and execute the union of focused tests and semantically required governed lanes. New in-scope paths must be classified in `dish/test_selection/ownership.csv`. Run the ordinary full suite before final delivery of a completed change block. Testing policy and evidence boundaries are in `dish/docs/testing.md` and `dish/docs/architecture/testing-boundaries.md`.

Do not package `.venv`, test caches, or generated test artifacts. Do not add runtime mutation paths, duplicate workflow authority in transports or CLIs, or preserve compatibility without a real producer or database-preservation requirement.

While doing assigned work, flag material maintainability or correctness issues you encounter that are relevant to the area you are touching: architecture/documentation gaps or contradictions that could cause mistakes; rules that do not match the actual code; likely bugs or weak/misleading tests; duplicated authority; significant code smells; brittle tooling; or recurring development friction. Do not broaden the implementation merely to fix unrelated observations. Report confirmed defects separately from suspicions and include enough concrete context for later triage.

## External instruction sources

`dish/deploy/gpt-action.md` contains a template, not the deployed custom GPT instructions. Changes to that template require a separate synchronized change in the live instructions repository and an explicit notice to Marco. If work changes the protocol's own structure, canonical fields, process records, or change classes, read `~/honest-pantry/dish-docs-design.md` and the relevant current Honest protocol/schema assets first.

## Memory

Do not create or update persistent memory files while working in this repository.
