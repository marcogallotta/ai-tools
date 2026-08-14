# ChatGPT Project role kernels

This directory owns the concise ChatGPT Project instruction kernels for recurring Dish roles. The kernels are a bootstrap/enforcement layer; detailed policy remains canonical in root `CLAUDE.md`, `dish/docs/agents/index.md`, and the mapped standing role contract. Do not turn Project instructions into a duplicated policy manual.

## Canonical topology

The current standing role index maps to eight persistent Project boundaries:

1. Coordinator
2. Development Workflow
3. Implementation
4. Review
5. Integration
6. Workflow specialist
7. PostgreSQL / Dark Launch specialist
8. Audit

`source.json` is the canonical data source for shared kernel rules, permitted role composition, role-specific high-consequence gates, and each rule's default drift impact/surface/action boundaries. `manifest.json` records the source digest, rendered-kernel identity, and a machine-readable `change_history` between canonical versions. The `canonical_version` binds both rendered instructions and rule-impact metadata, so behaviorally meaningful bootstrap or drift-classification changes move the version. The manifest also maps each role to its generated Markdown kernel. `chatgpt_project_kernels.py` fails if the source topology differs from the current standing role index, so role-map changes cannot silently leave obsolete Project kernels behind.

The generated role Markdown files are copyable Project-instruction text. Do not hand-edit them; change `source.json`, update the manifest version identity, and regenerate.

## Version and drift control

Each generated kernel declares `PROJECT_CANONICAL_VERSION`. A mismatch is a trigger to inspect semantic drift, not a blocker by itself. The Project reads `manifest.json`, folds every `change_history` transition from its declared version to current, then scopes changes to its role and current action boundary.

- **BREAKING**: stop only when the change affects this role and current action boundary; resynchronize the affected Project before that action.
- **ADDITIVE**: continue under current repository authority, apply the new rule when its boundary is reached, and defer Project resync to a natural boundary.
- **COMPATIBLE**: continue; wording, examples, diagnostics, and output-shape improvements do not hard-stop work.
- **UNRELATED**: a change for another Project/role does not block or require resync for this Project.

Skipped versions are folded transitively and the highest relevant impact wins. A missing history chain fails closed before role-critical writes. Explicitly unclassified authority/safety/lifecycle changes also fail closed; an unclassified presentation-only change is treated as compatible rather than automatically breaking.

The model is not asked to hash hidden UI instruction text. Repository validation binds the source, rendered instruction set, and rule-impact metadata; the live comparison uses the visible canonical version plus the manifest change chain.

Commands from the repository root:

```sh
python3 dish/scripts/chatgpt_project_kernels.py render
python3 dish/scripts/chatgpt_project_kernels.py render --check
python3 dish/scripts/chatgpt_project_kernels.py check
python3 dish/scripts/chatgpt_project_kernels.py prepare-eval --output /tmp/chatgpt-project-eval-cases.json
python3 dish/scripts/chatgpt_project_kernels.py eval --results /tmp/chatgpt-project-eval-results.json
python3 dish/scripts/chatgpt_project_kernels.py eval --runner-command '<fresh-chat-runner>' --save-results /tmp/chatgpt-project-eval-results.json
python3 dish/scripts/chatgpt_project_kernels.py version --project-version <declared-version> --role <role-key> --action-boundary <boundary>
```

`check` validates source/rendered-version binding, current role topology, generated files, character budget, and the complete approved eval contract set. It does **not** report behavioral adherence. `prepare-eval` emits oracle-free cases containing the exact current Project kernel and prompt. `eval` then judges structured results from one newly created ChatGPT Project chat per case, either from a recorded result bundle or from an operator-supplied runner command that is invoked separately for every case.

The behavior-v2 runner protocol separates the assistant's declared outcome/actions from **runner-observed evidence**. For scenarios that require external side effects, `evals.json` contains a hidden observation oracle. The runner must capture actual tool-layer events such as capability discovery, a durable GitHub write, and authoritative readback, including exact PR/head/write identity where required. Assistant-authored text such as “I submitted the review” is never observation evidence. The evaluator rejects missing, wrong-head, wrong-transport, mismatched-write, or out-of-order evidence even when the assistant declares every expected action label.

A runner is therefore part of the trusted eval boundary: it must instrument the fresh ChatGPT Project interaction and report tool observations independently of assistant prose. The repository harness can validate the recorded trace and link/readback invariants; it cannot cryptographically prove that an external runner fabricated no events.

## Acceptance and rollout policy

The repository keeps the complete approved matrix — 47 scenarios / 62 role-expanded cases — as deterministic harness coverage. `prepare-eval` emits all 62 cases, and action-bearing cases keep their machine-verifiable observation requirements.

The **complete live 62-case run is an automated/periodic regression target, not a mandatory manual merge gate**. Absence of an authorized fresh-Project runner or full live result bundle does not by itself make a repository change unreviewable or require manual recreation of the matrix.

Repository changes land on governed repository evidence and exact-head review requirements. When an automated live runner is available, use the full matrix for periodic regression and record failures as concrete follow-up defects.

Project UI resync scope follows the manifest's affected roles and impact. A relevant BREAKING change requires resync of only the affected Project(s) before the affected action; ADDITIVE changes defer resync; COMPATIBLE and UNRELATED changes do not hard-stop work. Audit is a new Project boundary in this version. Existing Projects follow the current manifest edge: only roles with relevant BREAKING changes must resynchronize before the affected action, while ADDITIVE-only changes may wait for a natural boundary. Future ordinary role-policy changes fetched from live Git authority do not imply an all-Project resync.

If a real ChatGPT Project rollout boundary genuinely needs live smoke validation, keep it deliberately small: at most one representative decision-only case and one representative action-bearing case on a safe/disposable test surface. The purpose is to confirm Project wiring and tool-observation instrumentation, **not** to claim exhaustive model-behavior certification. Such a smoke must be explicitly justified by the rollout risk; it is not a standing requirement for every kernel change.

## OpenAI product assumptions

Verified against the official OpenAI Projects documentation on 2026-08-12:

- Project-specific instructions apply inside a Project and override global custom instructions there.
- Project-only memory is an isolation option selected when creating a new Project; chats can reference other chats in that Project but not conversations outside it.
- Existing Projects cannot be converted to project-only memory in place; create a new Project when that isolation boundary is required.

Official source: <https://help.openai.com/en/articles/10169521-projects-in-chatgpt>.

No live ChatGPT Project is created or configured by this repository mechanism. Project-memory/history is context, not detailed policy authority; repository role contracts remain canonical.
