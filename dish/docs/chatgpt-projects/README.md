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

The generated role Markdown files are copyable Project-instruction text. `worker.md` is also generated from `source.json` as a first-class execution profile without becoming a standing semantic role. Do not hand-edit generated Project files; change `source.json`, update the manifest version identity, and regenerate.

`dish/docs/agents/design-principles.md` is the canonical detailed Design Principles document. Its stable DP-01…DP-10 bootstrap sentences are digest-bound by `source.json` and compiled into the shared `design-principles-bootstrap` rule. The generator writes that same concise projection into every current Project kernel and the current role index; those generated surfaces are projections, not second policy authorities.

## Progressive disclosure

Project kernels keep universal/high-consequence policy directly loaded and deliver conditional policy through an always-loaded trigger index. Canonical rules remain in `source.json`; every effective rule is mechanically classified as `DIRECT_ALWAYS_ON` or `TRIGGERED_READ`. A triggered rule must resolve through existing `context_dependencies` metadata to one or more exact `path#H2 heading` destinations, and generation fails if the trigger or bounded section is missing. The shared Work-chat presentation contract is not duplicated into every Project payload: the Project bootstrap points to the generated `## Work chat` block in root `CLAUDE.md`, which startup already requires reading before substantive work. This keeps the Project surface bounded without weakening role/lifecycle rules or creating a second policy source.

`manifest.max_project_settings_chars` is a repository compatibility budget for the **exact installable Project-settings payload**, not a published OpenAI Projects limit. Its current value is **8,000 characters**, inherited from the repository's existing conservative operating assumption. The canonical composer validates the actual production or TEST base plus any supplied reserved fast-track overlay, owns the exact separator/serialization, and reports base-kernel, TEST-metadata, overlay, total, remaining, and excess character counts. Kernel length alone is diagnostic and can never make an oversized composed payload green.

Generation also checks a deterministic supported-composition fixture for every persistent role: production, TEST, production plus one current-gate fast-track overlay, and TEST plus that overlay must all fit the current compatibility budget. Arbitrary TEST identity strings or overlay reason text are still checked at their actual size when composed; they are never truncated or promised unlimited headroom. Changing the numeric compatibility budget away from 8,000 requires explicit evidence from either a bounded empirical Project save/load/readback or an official Project-limit publication. Live Project/browser proof is therefore not a standing gate for ordinary changes that remain under the existing budget.

## Production and TEST freshness

Production kernels declare `PROJECT_CHANNEL: production`. On startup/re-grounding, a governed Project resolves current GitHub `main`, fetches the latest generated kernel for its declared role, and reads the current role index/contract and manifest from that same authority. Installed Project settings remain a strong bootstrap/version witness until current Git is grounded; afterward current Git kernel + role authority govern. Compatible/additive drift does not require manual Project resync, while unreadable or role-mismatched current authority fails closed only the affected action.

A TEST kernel is explicitly `PROJECT_CHANNEL: test` and binds an exact candidate version, PR, ref, 40-hex head, and candidate-manifest digest. A TEST start/re-ground must verify that exact binding and fail closed on movement/mismatch rather than chasing a new head. Candidate instruction behavior never expands current production role/mutation/Review/Integration/deployment authority, and TEST acceptance alone never promotes production. The dedicated TEST Project/canary sequence remains specific to the explicitly approved Chatty rollout rather than a standing gate for every kernel change.

## Version and drift control

Each generated kernel declares `PROJECT_CANONICAL_VERSION`. Exact-current Projects emit no Project-settings prefix. A version mismatch is only a trigger to inspect semantic history; it never blocks by itself. The Project folds every manifest transition from its declared version to current, then scopes each change to the exact role and action boundary before deciding.

The user-facing state is deterministic:

- `PROJECT SETTINGS: OUTDATED · DRIFT 1/3` — only COMPATIBLE or UNRELATED drift applies. Continue under current repository authority; Project resync is not required.
- `PROJECT SETTINGS: OUTDATED · DRIFT 2/3` — at least one applicable ADDITIVE change exists and no proven BREAKING incompatibility applies. Continue, applying the additive rule at its boundary; Project resync is not required.
- `PROJECT SETTINGS: HARD BREAK · DRIFT 3/3` — an applicable BREAKING incompatibility is proven for the exact prior Project version, role, and action. Stop only that affected action and follow the approved migration/resynchronization path.
- `PROJECT SETTINGS: INTEGRITY ERROR · DRIFT ?/3` — the history is missing/malformed/unclassifiable, or a claimed BREAKING transition lacks its required incompatibility proof. Fail closed only the affected action for **repository-authority repair**. Do not tell the operator to resynchronize the Project as a substitute for fixing repository metadata.

`resync_required` is therefore true only for an applicable proof-backed `DRIFT 3/3` transition (including the approved legacy bootstrap floor below). ADDITIVE, COMPATIBLE, UNRELATED, and INTEGRITY states never set it.

### BREAKING proof and compatibility-first rule

For drift-aware Projects, `impact: breaking` is exceptional. A retained BREAKING change must include machine-readable `break_proof` that names the exact prior Project version, exact roles and action boundaries, a concrete unsafe/uninterpretable old-kernel counterexample, why current Git authority cannot reconcile it safely, the approved migration/resync path, rollback path, and durable approval reference. The proof scope must exactly match the manifest change scope.

If the existing drift-aware kernel can safely load current Git authority and apply the new policy, a compatibility shim or nonblocking classification is mandatory instead of BREAKING. A historical change that was previously labeled BREAKING but fails that standard is reclassified with machine-readable `historical_correction` provenance (`previous_impact`, `provenance_ref`, and reason); history is not silently rewritten.

### Semantic-history floor

`dish-chatgpt-projects-v2-d96ab5f0588d` is the approved semantic-history floor: it is the first Project generation that can fold repository drift semantically. Projects older than that floor retain the legacy bootstrap hard break because their old mismatch logic cannot safely consume the semantic history mechanism itself. The manifest records the migration, rollback, and authority for that exception. Moving the floor later is itself a breaking change and requires the same proof discipline.

Skipped versions are folded transitively. Retained history may contain multiple predecessor generations converging on one later generation; each retained `from_version` still has one deterministic successor, so a Project version actually published on `main` is never discarded merely because a merged candidate carried a different predecessor. Known unrelated role/action changes are ignored before impact parsing, so malformed metadata for a demonstrably unrelated scoped change does not block another action; malformed scope that cannot be safely localized remains an integrity error.

`manifest.required_version_inventory` is the repository-owned completeness inventory for drift-aware Project versions legitimately published on first-parent `main`, plus the current candidate version. `check` requires every active inventory entry to be represented and to have one deterministic path to current canonical. A version can leave the active set only through an exact-version retirement record with durable explicit human authority; `historical_correction` can repair classification metadata but cannot retire or redirect topology. The inventory is deliberately independent of the retained graph, so deleting both a historical edge and its inventory entry is not a valid reconciliation.

Candidate publication must also pass authoritative-base admission. `admit` compares the candidate inventory with the current-base manifest and rejects any unretired required version lost from the candidate, including complete deletion or a stale strict subset. For ordinary source changes, `reconcile` is the supported manifest-generation path: it computes canonical identity and transition fingerprints with the same generator functions used by `check`, carries the authoritative inventory forward, and emits deterministic JSON. With a concurrent candidate manifest/source, it preserves authoritative-base history, converges compatible/additive disjoint lineages, and fails closed on ambiguous or incompatible overlapping rule histories. Do not hand-edit fingerprints or reconstruct `change_history` as the normal reconciliation path.

The model is not asked to hash hidden UI instruction text. Repository validation binds the source, rendered instruction set, rule-impact metadata, compatibility configuration, required-version inventory, and current transition fingerprints; the live comparison uses the visible canonical version plus the manifest history.

Commands from the repository root:

```sh
python3 dish/scripts/chatgpt_project_kernels.py render
python3 dish/scripts/chatgpt_project_kernels.py render --check
python3 dish/scripts/chatgpt_project_kernels.py check
python3 dish/scripts/chatgpt_project_kernels.py reconcile --base-manifest <current-main-manifest> --base-source <current-main-source> --source <candidate-source> --output <candidate-manifest>
python3 dish/scripts/chatgpt_project_kernels.py reconcile --base-manifest <current-main-manifest> --base-source <current-main-source> --candidate-manifest <concurrent-manifest> --candidate-source <concurrent-source> --source <resolved-source> --output <reconciled-manifest>
python3 dish/scripts/chatgpt_project_kernels.py admit --base-manifest <current-main-manifest> --base-source <current-main-source> --candidate-manifest <candidate-manifest> --candidate-source <candidate-source>
python3 dish/scripts/chatgpt_project_kernels.py prepare-eval --output /tmp/chatgpt-project-eval-cases.json
python3 dish/scripts/chatgpt_project_kernels.py eval --results /tmp/chatgpt-project-eval-results.json
python3 dish/scripts/chatgpt_project_kernels.py eval --runner-command '<fresh-chat-runner>' --save-results /tmp/chatgpt-project-eval-results.json
python3 dish/scripts/chatgpt_project_kernels.py version --project-version <declared-version> --role <role-key> --action-boundary <boundary>
```

`check` validates source/rendered-version binding, current role topology, generated files, the exact composed Project-settings compatibility budget (including supported production/TEST/overlay fixtures), semantic-history configuration, required-version reachability, current-edge classifications, proof/correction/retirement metadata, and the complete approved eval contract set. It does **not** report behavioral adherence. `prepare-eval` emits oracle-free cases containing the exact current Project kernel and prompt. `eval` judges structured results from one newly created ChatGPT Project chat per case, either from a recorded result bundle or from an operator-supplied runner command invoked separately for every case.

The behavior-v2 runner protocol separates the assistant's declared outcome/actions from **runner-observed evidence**. For scenarios that require external side effects, `evals.json` contains a hidden observation oracle. The runner must capture actual tool-layer events such as capability discovery, a durable GitHub write, and authoritative readback, including exact PR/head/write identity where required. Assistant-authored text such as “I submitted the review” is never observation evidence. The evaluator rejects missing, wrong-head, wrong-transport, mismatched-write, or out-of-order evidence even when the assistant declares every expected action label.

A runner is therefore part of the trusted eval boundary: it must instrument the fresh ChatGPT Project interaction and report tool observations independently of assistant prose. The repository harness can validate the recorded trace and link/readback invariants; it cannot cryptographically prove that an external runner fabricated no events.

## Acceptance and rollout policy

The repository keeps the complete approved matrix — currently 187 scenarios / 341 role-expanded cases — as deterministic harness coverage. `prepare-eval` emits the full matrix, and action-bearing cases keep their machine-verifiable observation requirements.

The **complete live matrix run is an automated/periodic regression target, not a mandatory manual merge gate**. Absence of an authorized fresh-Project runner or full live result bundle does not by itself make a repository change unreviewable or require manual recreation of the matrix.

Repository changes land on governed repository evidence and exact-head review requirements. When an automated live runner is available, use the full matrix for periodic regression and record failures as concrete follow-up defects.

Project UI resync scope follows the manifest's exact role/action classification and proof. Only proof-backed applicable BREAKING drift (or the approved pre-semantic-history legacy floor) requires resync before the affected action. ADDITIVE, COMPATIBLE, and UNRELATED changes continue without resync; INTEGRITY ERROR requires repository-authority repair rather than Project resync. Future ordinary role-policy changes fetched from live Git authority do not imply an all-Project resync.

If a real ChatGPT Project rollout boundary genuinely needs live smoke validation, keep it deliberately small: at most one representative decision-only case and one representative action-bearing case on a safe/disposable test surface. The purpose is to confirm Project wiring and tool-observation instrumentation, **not** to claim exhaustive model-behavior certification. Such a smoke must be explicitly justified by the rollout risk; it is not a standing requirement for every kernel change.

For the Chatty V1 rollout owned by Asana `1217505866209433`, bind every smoke to one exact unmerged candidate head. Claude Code and Codex each run exactly two fresh-session smokes from that candidate checkout: D1 is the representative decision/high-level-review case and A1 is the representative already-authorized safe action with observed action + readback. Both must pass per host. Only then may the dedicated ChatGPT TEST Project, once its separate TEST-candidate channel and instruction-space prerequisites are ready, run the same two semantic classes in two newly created chats. A candidate head change invalidates smoke evidence for the affected stage. These are bounded rollout smokes; the complete behavior-v2 matrix remains repository/periodic regression coverage rather than a manual promotion gate.

## OpenAI product assumptions

Verified against the official OpenAI Projects documentation on 2026-08-12:

- Project-specific instructions apply inside a Project and override global custom instructions there.
- Project-only memory is an isolation option selected when creating a new Project; chats can reference other chats in that Project but not conversations outside it.
- Existing Projects cannot be converted to project-only memory in place; create a new Project when that isolation boundary is required.

Official source: <https://help.openai.com/en/articles/10169521-projects-in-chatgpt>.

No live ChatGPT Project is created or configured by this repository mechanism. Project-memory/history is context, not detailed policy authority; repository role contracts remain canonical.
