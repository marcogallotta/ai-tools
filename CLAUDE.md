# ai-tools agent map

Read `README.md` for repository purpose and host integration. For every change under `dish/`, start at [`dish/docs/architecture/index.md`](dish/docs/architecture/index.md) and follow its task routing to the relevant ownership and invariant documents. Operational commands belong in runbooks; maintained architecture claims belong only in the architecture knowledge base.

## Agent roles

For Dish work, role routing lives in [`dish/docs/agents/index.md`](dish/docs/agents/index.md).

If you are told to assume, act as, or hand work to a named Dish role, read that index first and then the mapped standing role contract before acting. Do not infer role policy from a nearby file or repeat stable role rules in task handoffs.

Standing role contracts contain stable policy so task handoffs can stay short and contain only the task-specific delta. If a handoff conflicts with a standing role contract, flag the conflict rather than silently choosing a new policy.

For exact-reviewed-PR-head integration, local integration certification, commit/promotion to `main`, push verification, or integration-worktree cleanup, follow the dedicated Integration agent contract in `dish/docs/agents/integration.md`. Implementation/fix agents do not inherit final integration authority merely because they produced the implementation.

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

The ChatGPT GitHub connector and ChatGPT repository/dependency-bundle retrieval paths below are **not** standing instructions for Claude Code or Codex. Do not put connector setup, Actions-artifact bundle retrieval, or a user-supplied bundle into their handoffs unless Marco explicitly makes that a task-specific requirement.

### ChatGPT agents

For ChatGPT agents, use the connected GitHub integration as source/history authority for this private repository. A repository bundle is a verified bootstrap/cache only; it never overrides GitHub source/history.

For recurring ChatGPT Dish role Projects, the canonical concise Project kernels and version manifest live in [`dish/docs/chatgpt-projects/`](dish/docs/chatgpt-projects/README.md). At the first substantive action, compare the Project-declared `PROJECT_CANONICAL_VERSION` with the current repository manifest. On mismatch, report `PROJECT INSTRUCTIONS STALE` and make no role-critical state change until the Project instructions are resynchronized. Project kernels bootstrap critical gates; they never replace the current role index or standing role contract.

For substantial repository-changing work, use the repository bundle first:

- resolve the intended current `refs/heads/main` SHA and repository identity from GitHub authority;
- retrieve the exact matching Actions artifact named `repository-bundle-<SHA>`; never substitute a stale/newer/older bundle;
- materialize the artifact in the runtime and run `scripts/repository_bundle.py verify` against the authoritative repository full name/numeric identity, exact SHA, and `refs/heads/main`;
- use the verified local clone for `git log`, `git diff`, search/grep, tests, and local branch/commit preparation;
- fail closed if connector retrieval, materialization, checksum/manifest verification, `git bundle verify`, advertised-main validation, or cloned-HEAD validation fails.

The publication and verification contract, cadence, retention, and v1 main-only scope are in [`ci/repository-bundle.md`](ci/repository-bundle.md). GitHub Connect remains the live path for PR state, remote metadata, and small targeted source retrieval. Unless authenticated Git transport is deliberately added later, publish branch/commit/PR state through connector-native GitHub operations rather than assuming the verified clone can push.

Bootstrap Python runtime dependencies from the authoritative GitHub-built dependency bundle rather than asking Marco to upload a virtual environment or bundle manually:

- the versioned GitHub Release is publication authority for the bundle;
- the matching GitHub Actions artifact is the ChatGPT retrieval/evidence mirror and must carry the same bundle identity;
- resolve the current expected bundle identity from authoritative repository metadata/tooling rather than hard-coding a historical bundle ID;
- download through the GitHub connector, verify the manifest and checksum, and use `scripts/dependency_bundle.py install` to recreate the repository environments;
- preserve the bundle's fail-closed Python/platform/architecture/libc checks. For glibc, a runtime newer than the declared minimum is compatible; an older runtime is not;
- if connector access, the matching artifact, or runtime compatibility is unavailable, report that exact missing capability rather than falling back to an uploaded archived `.venv`.

These connector-and-bundle bootstraps are **ChatGPT-only policy**. They must not be copied into Claude Code or Codex handoffs.

On Marco's local development machine, the preferred reviewed fast path is `.venv/bin/python scripts/dish-test-lane parallel-safe --workers 4`, or the planner's equivalent `--parallel-workers 4` focused command. Four workers is a locally benchmarked recommendation, not a repository-wide hardware constant. Keep serial execution available for diagnosis and for every selection outside the currently qualified parallel-safe inventory.

Use the test planner for the complete changed-path set and execute the union of focused tests and semantically required governed lanes. New in-scope paths must be classified in `dish/test_selection/ownership.csv`. Run the ordinary full suite before final delivery of a completed change block. Testing policy and evidence boundaries are in `dish/docs/testing.md` and `dish/docs/architecture/testing-boundaries.md`.

Do not package `.venv`, test caches, or generated test artifacts. Do not add runtime mutation paths, duplicate workflow authority in transports or CLIs, or preserve compatibility without a real producer or database-preservation requirement.

While doing assigned work, flag material maintainability or correctness issues you encounter that are relevant to the area you are touching: architecture/documentation gaps or contradictions that could cause mistakes; rules that do not match the actual code; likely bugs or weak/misleading tests; duplicated authority; significant code smells; brittle tooling; or recurring development friction. Do not broaden the implementation merely to fix unrelated observations. Report confirmed defects separately from suspicions and include enough concrete context for later triage.

## External instruction sources

`dish/deploy/gpt-action.md` contains a template, not the deployed custom GPT instructions. Changes to that template require a separate synchronized change in the live instructions repository and an explicit notice to Marco. If work changes the protocol's own structure, canonical fields, process records, or change classes, read `~/honest-pantry/dish-docs-design.md` and the relevant current Honest protocol/schema assets first.

## Memory

Do not create or update persistent memory files while working in this repository.
