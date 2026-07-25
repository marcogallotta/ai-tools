# Dish Tool Update Analysis

**Scope:** compare the dish-tool implementation and documentation in `ai-tools(21)` with the frozen dish protocols in `honest(147)`.

**Mode:** analysis and planning only. No tool code, existing tool documentation, or protocol file was changed. Explicit, versioned protocol amendments remain allowed where they make the implementation cleaner or safer.

## Sources reviewed

**Frozen authority**

- `honest(147)/CLAUDE.md`
- `dish-planning-protocol.md`
- `dish-research-protocol.md`
- `dish-verification-protocol.md`
- `dish-cooking-protocol.md`

**Current tool and documentation**

- `ai-tools(21)/CLAUDE.md`
- `bin/dish`, `bin/dish-admin`, and `bin/dish_tool/*.py`
- `bin/dish-tool-imp.md`
- `bin/dish-tool.md`
- `bin/dish-tool-activation.md`
- `bin/dish-chatgpt-relay.md`
- `bin/dish-reports.sql`
- dish-tool tests and fixture protocol-release assets under `bin/tests/`

## Executive finding

The current dish tool is not compatible with the frozen protocols. It implements an older lifecycle in which a detached candidate file and local database can remain the working authority, followed by one final Asana write, opposite model-family verification, a legacy `Verification:` field, obsolete Small/Large correction routes, automatic second-rejection escalation, and one protocol/schema bundle frozen at submission start.

That release bundle is not random machinery: the old design deliberately used it to keep protocol text, manifests, and validator behaviour aligned. The mismatch is that it pins each task to the whole bundle selected at `dish start`. The settled replacement is a repository-level compatibility check, not task-lifetime protocol pinning: `honest` owns the current protocols, machine schema, migrations, and an uppercase `DISH_VERSION` file declaring `PROTOCOL_VERSION` and `SCHEMA_VERSION`; `ai-tools` is a generic engine that runs only when it supports those exact current versions. Each task stores only its `Schema version` for data compatibility. The separate `Verification protocol release` remains cycle-specific and is frozen whenever the live task enters `pending-verification`.

The protocols also require the exact live Asana task to be the candidate handed off, independently verified, and signed; a seven-field task-native state block; a fresh independent ChatGPT verification run; verifier-owned Small and Large corrections; separate Evidence and Human Review states; exact-content signoff; and movement rules independent of readiness. Ephemeral local drafting files remain permissible, but they cannot substitute for the live task or become the object of signoff.

**Do not activate the current tool on protocol-managed tasks until these areas are redesigned and retested.** This is an architectural compatibility update, not a terminology patch.

## Settled rollout decisions

These decisions are approved for the first compatible rollout. They may later change through an explicit, versioned protocol/schema revision.

1. `honest` owns the governing protocol prose, machine-readable task schema, schema migrations, and an uppercase `DISH_VERSION` file containing `PROTOCOL_VERSION=<version>` and `SCHEMA_VERSION=<version>`.
2. `ai-tools` contains the generic validation/workflow engine. It reads the protocol version, schema version, and schema from the checked-out `honest` repository; it does not duplicate the dish schema as its own authority.
3. `ai-tools` runs only when it supports the exact current `PROTOCOL_VERSION` and `SCHEMA_VERSION` declared by `honest`. A mismatch fails closed with a clear compatibility error.
4. Each live task stores only its `Schema version`. This is a data-compatibility marker, not a permanent pin to old protocol behaviour. Current protocol prose governs all active tasks.
5. A schema change requires a schema-version bump and a migration, preferably scripted. An older-schema task is refused for normal operations with `migration required`; migration is explicit, never silently automatic.
6. A migration is complete only after the transformed live task is written, reread, and validated. Only then is its `Schema version` updated. A failed migration must leave the task on its prior schema version.
7. A protocol change always bumps `PROTOCOL_VERSION`. A schema change also bumps `SCHEMA_VERSION`; a protocol-only change need not change the schema version. `bin/git-commit` must inspect staged changes to governed protocol, schema, and migration files and flag a missing required bump before commit. Automatic bumping may be added only where the required bump is unambiguous; the minimum V1 requirement is a blocking confirmation/check.
8. The machine schema is traceable clause-by-clause to the governing prose. Prose wins over schema or tool output; disagreement fails closed.
9. The task field `Verification protocol release` is separate from both version numbers. It records the exact Verification-protocol text used for that Verification cycle.
10. Destination is serialized as `Destination section: <section name> — <section gid>`.
11. A platform run/session ID is stored when available. Otherwise a tool-recorded independence attestation is sufficient. The verifier must not have constructed or materially edited the candidate.
12. Other agents may assist with retrieval or mechanical work, but the recorded constructor or material editor must be the ChatGPT run that reviews the exact candidate.
13. Every task-body edit invalidates exact-content signoff. Even a typo opens a fresh Verification cycle.
14. A two-pass reset requires a new `Material changes` entry naming category (`evidence`, `premise`, `method`, or `scope`), concrete before/after change, editor, and date. A new hash alone is insufficient.
15. Role classification is binary. Main is the unmarked default. Only non-main tasks carry `[non-main]`; main-dish nutrition requirements apply only to main tasks. This requires explicit amendments to the current protocol wording before activation.
16. Missing or invalid destination remains visible in both the title and `Destination section`, using `[destination missing]` or `[destination invalid]`. It blocks only the final move.
17. Lower-level subheadings are allowed inside canonical sections. Canonical top-level headings and Process Record labels remain fixed.
18. Human decisions use `Human — Marco: <decision>; scope: <scope>; date: <YYYY-MM-DD>; reason: <reason>`.
19. Source records use `Construction | Later validation — <source and locator> — status: used | conflicting | rejected — affects: <claim, ratio, method, or adaptation> — route: <chosen treatment> — reason: <reason or limitation> — future test: <test or None>`. The overall Research-basis classification—`Source-backed dish`, `Halal port`, or `Intentional test dish`—must also remain explicit.
20. Material changes use `<YYYY-MM-DD> — ChatGPT — <model>: <concrete change>; reason: <reason>; verification: <state>; resulting version: <version ID>`. Two-pass resets append category and before/after details.
21. The non-Git Verification-release form is `sha256:<64 lowercase hex>; read-at=<RFC3339 UTC timestamp>`.
22. Nonterminal legacy tasks are quarantined and reconciled individually. Never infer `ready`; keep the old project untouched until migration is accepted.
23. Planning, Research, and Verification have mandatory deterministic tool checks at defined phase boundaries. Their protocols own when checks are required and the agent's remaining semantic duties; `dish-tool-activation.md` owns commands, environment, arguments, output/exit semantics, and troubleshooting. A tool pass never authorizes a protocol transition or signoff by itself.
24. Behaviour for a genuinely breaking protocol change while a submission is already open is deliberately deferred until such a change is encountered. Until that policy exists, protocol changes made while submissions are open must be limited to backward-compatible/minor changes; do not invent automatic restart, rebind, or migration behaviour.
25. The shared service remains a V1 multi-agent go-live requirement. GPT Action connectivity is settled: Tailscale Funnel exposes the laptop-hosted service publicly, the Action authenticates with its own dedicated bearer token scoped only to the dish endpoints it may call (never shared with any CLI/admin credential), and it consumes a trimmed OpenAPI document exposing only that scoped surface — the same per-audience-token/Funnel/trimmed-OpenAPI pattern already used by `plant-monitoring`'s Assistant API (`ASSISTANT_API_TOKEN`, `/assistant-openapi.json`; see `~/plant-monitoring/docs/internals.md`).

## Requirement levels used below

- **Protocol-required behaviour:** an externally observable rule that must hold for conformance, regardless of internal design.
- **Approved rollout decision:** a convention explicitly settled for this rollout, including any versioned protocol amendment named above.
- **Recommended implementation:** the preferred engineering design, but an alternative is acceptable if it satisfies the required behaviour and acceptance criteria without weakening auditability or future migration.

Where a `Required change` bullet names a specific storage object or table shape, treat that shape as recommended unless the bullet is explicitly identified as an approved rollout decision.

---

# Consolidated mismatch register

Each item below consolidates related entries from the original 40-item analysis while preserving every material issue.

## C-01 — Task-pinned release bundle versus current compatibility model

**Covers:** original M-01 to M-03.

**Mismatch**

The existing release machinery is deliberate and load-bearing for the old design. At `dish start` it resolves a bundle of Planning, Research, and Verification protocols plus manifests and freezes that bundle for the submission's lifetime. This ensures internal consistency, but it also binds a task to old substantive protocol behaviour merely because the task started earlier.

The settled model keeps the safety check but removes task-lifetime protocol pinning. `honest` is the current source of truth for protocols and machine schema. `ai-tools` is a generic engine that checks compatibility with the exact current versions before running. Tasks carry only a schema-compatibility version and are migrated when their stored structure is old. The Verification protocol remains separately frozen per Verification cycle because the protocol explicitly requires that audit record.

**Affected locations**

- `bin/git-commit`
- `bin/dish_tool/constants.py`
- `bin/dish_tool/releases.py`
- release creation and reuse in `bin/dish_tool/commands.py`
- release columns in `bin/dish_tool/database.py`
- fixture manifests under `bin/tests/`
- `bin/dish-tool-imp.md`, `bin/dish-tool.md`, and `bin/dish-tool-activation.md`

**Governing requirement and approved rollout model**

- Current protocol prose governs all active tasks; a task is not permanently pinned to the protocol version present when it was created.
- `honest/DISH_VERSION` declares `PROTOCOL_VERSION` and `SCHEMA_VERSION`.
- The machine-readable task schema and migrations live with the protocols in `honest`.
- `ai-tools` runs only when it supports the exact versions declared by `honest`.
- Each task stores only `Schema version`; older-schema tasks require explicit migration.
- Every entry into `pending-verification` freezes the then-current exact Verification protocol in the task's `Verification protocol release` field.

**Required change**

**Protocol-required behaviour**

- Preserve fail-closed consistency between governing prose, machine schema, and tool capability.
- Stop freezing the whole protocol bundle per task at `dish start`.
- Do not store a general task-level protocol release that causes old Planning or Research rules to persist.
- On every transition into `pending-verification`, resolve the exact current Verification protocol and write its identity to `Verification protocol release`.
- Never populate `Verification protocol release` from `PROTOCOL_VERSION`, `SCHEMA_VERSION`, a wrapper bundle name, or a start-time release value.

**Approved rollout implementation**

- Add uppercase `honest/DISH_VERSION`:

  ```text
  PROTOCOL_VERSION=<version>
  SCHEMA_VERSION=<version>
  ```

- Move the authoritative machine schema from `ai-tools` fixtures/configuration into `honest`; keep validation code in `ai-tools`.
- Make `ai-tools` declare and enforce exact supported protocol/schema versions.
- Update `bin/git-commit` to inspect staged changes to governed protocol, schema, and migration files. If the corresponding `PROTOCOL_VERSION` and/or `SCHEMA_VERSION` bump is absent, stop or require an explicit confirmation before commit. Prefer flagging over silent auto-bumping in V1; automatic bumping is acceptable later only when the correct bump is deterministic.
- Add `Schema version: <version>` as a required canonical task-body metadata field. It is separate from, and does not expand, the protocol's seven-field state block. Do not represent it as a subtask or Asana custom field.
- Refuse normal operations on an older-schema task with a clear `migration required` result.
- Require explicit, preferably scripted migrations. Write, reread, and validate the transformed live task before updating its `Schema version`; failure leaves the old version intact.
- Retain old bundle/release fields only for legacy reading, reporting, or migration; they are not authoritative for new work.

**Human input:** no immediate blocker. The policy for a genuinely breaking protocol change while a submission is already open is intentionally deferred. Until it is defined, restrict in-flight protocol changes to backward-compatible/minor changes. Exact filenames for the schema and migration scripts are implementation details.

## C-02 — Live-task authority, exact-content versions, and write lifecycle

**Covers:** M-04 to M-06, M-25, and M-32.

**Mismatch**

The current tool treats a local candidate file and SQLite row as the working authority, may move a task to Verification Queue before writing the candidate, and performs one final title/notes write after approval. Approval is not bound to the exact content later submitted, and external edits are not reliably detected.

**Affected locations**

- `prepare()`, `_prepare_move_only()`, `approve()`, `submit()`, and candidate loading in `bin/dish_tool/commands.py`
- submission/write fields in `bin/dish_tool/database.py`
- generic-write advisory logic in `bin/dish_tool/advisory.py`
- lifecycle descriptions in `bin/dish-tool.md` and `bin/dish-tool-imp.md`

**Governing requirement**

The exact live Asana title/body is the candidate constructed, handed off, verified, signed, and later cooked. Agents access that live task only through the dish tool; the tool mediates every read, write, check-in, correction, signoff, and move. Signoff applies only to the exact final content.

**Required change**

**Protocol-required behaviour**

- Make the live Asana task authoritative once the canonical task exists. Agents must not access or mutate the task except through the dish tool. SQLite and local files may stage edits or retain local diagnostics, but neither is the content authority or the object of Verification.
- Permit an ephemeral local candidate file only as a tool-mediated editing input. The tool must write the exact candidate to Asana, re-read it, and bind Verification to that live content before handoff or signoff.
- Persist an exact title/body identity and sufficient history to prove which content was read, corrected, self-reviewed, signed, and later changed.
- Write and re-read the complete `pending-verification` task before Research Queue → Verification Queue movement.
- Bind verifier read, correction, approval, and final write to the same exact-content identity.
- Detect any out-of-band Asana edit, including manual web edits or another tool instance, and invalidate stale operations/signoff. Direct agent access is not a supported workflow.
- Split signoff and movement into separate recoverable operations.

**Recommended implementation**

- Serialize mutations through the shared task lock. Ordinary full-title/full-notes writes and approval retries should be naturally idempotent: if the exact intended content and state are already live, return success without appending duplicate provenance or repeating side effects.
- Use title/body hashes or equivalent snapshots for exact-content identity, signoff binding, drift detection, history, and recovery. These identities are audit/conformance controls, not a substitute for the shared lock and not a required per-request `expected_version`.

**V1 staging decision:** local single-agent testing may proceed without a shared service, provided only one agent operates the tool at a time and the limitation is explicit. Proper multi-agent go-live requires one shared dish service, hosted initially on Marco's laptop, to own the lock/lease, shared submission state, and all Asana access. GPT Actions and the CLI must be clients of that same service. GPT Action connectivity is settled: Tailscale Funnel exposes the service, the Action authenticates with its own dedicated bearer token scoped only to the endpoints it may call, and it consumes a trimmed OpenAPI document limited to that scoped surface — the pattern already proven by `plant-monitoring`'s Assistant API (see `~/plant-monitoring/docs/internals.md`). SQLite inside copied repositories must not be presented as a cross-agent lock.

**Human input:** none. The exact service framework and lock implementation are implementation details. Normalization should be limited to transport-proven CRLF/LF differences unless sandbox testing proves other Asana normalization.

## C-03 — Authoritative task state and cooking readiness

**Covers:** M-07, M-08, and M-31.

**Mismatch**

The implementation and fixtures rely on an obsolete `Verification:` line and tool-local states such as `drafting`, `awaiting_verification`, and `awaiting_human`. They do not parse or validate the protocol’s seven-field state block, and tool-local `ready` can exist before a valid live task is written or independently verified.

**Affected locations**

- state constants and transition checks in `bin/dish_tool/constants.py` and `commands.py`
- schema and lifecycle fields in `bin/dish_tool/database.py`
- fixture manifests and tests containing `Verification:`
- readiness descriptions in `bin/dish-tool.md`

**Governing requirement**

The canonical task contains exactly:

- `Status`
- `Status detail`
- `Resume status`
- `Verification protocol release`
- `Researched by`
- `Verified by`
- `Self-verified`

Cooking proceeds only from the exact live task at `Status: ready` with valid completed provenance.

**Required change**

- Delete new-work support for `Verification:`; retain old text only as quarantined audit context.
- Implement exact-once parsing, rendering, complete-block rewriting, and legal-combination validation for all seven fields; re-read the live task after each state transition.
- Keep internal operation state separate from authoritative task state.
- Reserve user-facing `ready` for a re-read live task whose exact content, state, release, self-review, and independent signoff are valid.

**Human input:** none.

## C-04 — Stage routing, model routing, and actor identity

**Covers:** M-09 to M-12 and M-34.

**Mismatch**

Research receives the Verification protocol; Verification is selected by opposite `claude` versus `gpt/codex` family; the tool records only coarse family tokens; and the relay describes file handoff to an opposite-family verifier. The implementation cannot prove a fresh independent ChatGPT run or preserve original constructor versus later material editor correctly.

**Affected locations**

- protocol bundling in `bin/dish_tool/models.py` and `commands.py`
- agent/family constants, CLI options, database fields, and verifier checks
- `bin/dish-chatgpt-relay.md`
- routing sections in `bin/dish-tool.md` and `bin/dish-tool-imp.md`

**Governing requirement**

- Planning reads Planning protocol only.
- Research reads Research protocol and, when a canonical task exists, the live task; it does not receive Verification protocol.
- Verification reads Verification protocol and the exact live task; it does not receive Research protocol.
- Independent signoff is by a fresh ChatGPT run that did not construct or materially edit the candidate.

**Required change**

- Remove opposite-family routing from protocol compliance.
- Route stage-specific protocol text only.
- Persist exact model, run/session ID when available, fallback attestation, actor role, content version, material-edit flag, and signoff date.
- Preserve `Researched by` as the original constructor; set `Self-verified` to the latest material editor only after exact-content review; keep `Verified by` clear until independent signoff.
- Rewrite the relay after the implementation stabilizes.

**Human input:** none; actor scope and fallback proof are settled.

## C-05 — Small, Large, post-signoff, and two-pass correction behaviour

**Covers:** M-13 to M-15 and M-18.

**Mismatch**

The current workflow lets Small submissions bypass independent Verification, sends Large corrections back to generic drafting/ownership transfer, does not reset ready tasks correctly after body edits, and automatically turns a second rejection into Human Review.

**Affected locations**

- Small/Large paths in `start()`, `prepare()`, `approve()`, `reject()`, and `submit()`
- `--level`, `--correction`, and `--take-ownership` CLI options
- failed-pass and rejection fields in `database.py`
- correction documentation and reports

**Governing requirement**

- Small is a verifier finding: the verifier fixes, self-reviews, rechecks, and signs the exact final task in the same pass.
- Large is verifier-owned correction work, followed by a new Verification cycle and a different fresh verifier.
- Every post-signoff body edit opens a new cycle.
- Two failed independent passes stop further identical-method attempts until evidence, premise, method, or scope changes; this is not automatically Human Review.

**Required change**

- Remove Small-as-bypass.
- Implement Small as verifier-owned live-task correction, exact-content self-review, deterministic recheck, and independent signoff in the same pass. Use one recoverable transaction where practical.
- Implement Large as: record defects → fix all resolvable defects → record material changes → self-review → clear signer → freeze new release → leave `pending-verification` → require another fresh verifier.
- Apply the same reset to every post-signoff body edit.
- Track pass count by candidate lineage and require the approved structured reset evidence before continuing after two failed passes.

**Human input:** none.

## C-06 — Evidence, Human Review, and governed decisions

**Covers:** M-16, M-17, and M-28.

**Mismatch**

The current tool lacks `pending-evidence`, uses `awaiting_human` as a mechanical rejection state, resumes to generic drafting, and treats exemption revision as a CLI bypass rather than a governed route decision.

**Affected locations**

- lifecycle constants and database schema
- `reject()` and admin unblock logic
- `bin/dish-admin`, `bin/dish_tool/admin.py`, and `admin_cli.py`
- exemption handling in `commands.py`
- associated tool documentation

**Governing requirement**

- `pending-evidence` is only for a material factual input that Marco must supply.
- `pending-human-review` is only for a material preference, authorization, classification, or accepted-risk decision.
- Both states record the interrupted phase in `Resume status` and resume that phase after resolution.
- Material changes to locked purpose, explicit locks/exemptions, or another approved route require a recorded Human decision; not every edit to any Planning field does.

**Required change**

- Add Evidence records containing the exact missing fact, why it matters, request to Marco, supplied evidence/source/date, and resume phase.
- Add Human Review records containing the exact decision, alternatives/consequence where relevant, resume phase, and the approved canonical decision line.
- Preserve a Verification release only when resuming an interrupted unchanged Verification cycle.
- Remove superseded routes after a decision.
- Replace exemption-revision bypasses with a real Human Review transition when the change is materially outside approved Planning bounds.

**Human input:** only the dish-specific decision or evidence itself.

## C-07 — Planning preservation and canonical task structure

**Covers:** M-19 to M-21 and part of M-30.

**Mismatch**

The fixture Planning schema is obsolete, only some Planning data is preserved, and the complete-task validator does not match the canonical task shape or distinguish top-level headings from permitted subheadings.

**Affected locations**

- `bin/tests/fixtures/protocol-release/dish-planning-manifest.json`
- structural parsers in `bin/dish_tool/validation.py`
- Planning snapshots in `commands.py` and `database.py`
- structural rules in `bin/dish-tool.md` and `bin/dish-tool-imp.md`

**Governing requirement**

The Planning brief has exactly six fields:

- `Dish candidate`
- `Purpose`
- `Locks`
- `Exemptions`
- `Research emphasis`
- `Destination section`

The canonical task has fixed top-level sections, a divider between cooking brief and Process Record, fixed Process Record labels, and no invented top-level sections or exemption tags.

**Required change**

- Replace the old Planning manifest with exact parsing/rendering for the six fields.
- Preserve all six through Research and reconcile them with the final title, brief, quantities, Decisions, and blockers.
- Treat Dish candidate, Purpose, explicit Locks, and Exemptions according to their protocol lock semantics; do not automatically escalate harmless destination or emphasis refinement.
- Validate canonical top-level structure and divider.
- Allow arbitrary lower-level subheadings inside canonical sections, but reject added top-level sections or invented Process Record labels.
- Reject placeholders, broken citations, or internal structural contradictions in a signable candidate.

**Human input:** none except a real material route change.

## C-08 — Evidence, Research basis, provenance, and `Material changes`

**Covers:** M-22, M-23, and part of M-30.

**Mismatch**

The tool validates shape more than support. It does not represent direct claim support, source disagreement, rejected sources, access limitations, overall Research-basis classification, or task-native material-edit provenance. Its operational audit cannot substitute for the canonical task record.

**Affected locations**

- evidence and validation paths in `bin/dish_tool/validation.py` and `commands.py`
- audit schema in `database.py`
- fixture manifests and documentation

**Governing requirement**

Material claims must have direct support; disagreements and rejected routes must remain visible; broken/placeholder citations are missing support; Research gaps are routed according to who can resolve them; every material task-body edit is recorded in `Material changes`.

**Required change**

**Protocol-required behaviour and approved rollout decisions**

- Require one overall Research-basis classification: `Source-backed dish`, `Halal port`, or `Intentional test dish`.
- Parse and validate the approved structured source-record grammar, including used/conflicting/rejected status, affected claim, chosen route, limitation/reason, and future test where applicable.
- Preserve enough structured provenance to check locators, source status, affected claims, chosen routes, limitations, and routing integrity. Semantic support quality still requires ChatGPT Verification.
- Require the approved `Material changes` line for every body edit and two-pass reset, tied to the resulting exact-content identity.

**Recommended implementation**

- Normalize evidence, claims, and claim/source links into separate records and associate them with immutable content versions. A simpler initial representation is acceptable if it preserves all canonical task text and supports the required integrity checks without lossy reconstruction.

**Human input:** none.

## C-09 — Title grammar and nutrition scope

**Covers:** M-29 and the title-related parts of M-30 and M-38.

**Mismatch**

The old manifests use a larger role-tag vocabulary and `[blockers unreviewed]`; the current frozen Research wording forbids `[non-main]`; and nutrition rules are not scoped to the approved main/non-main distinction.

**Affected locations**

- title parsing in `bin/dish_tool/validation.py`
- complete-task fixture manifest and title tests
- title assumptions in `bin/dish-tool-activation.md` and `bin/dish-tool-imp.md`
- Planning, Research, and Verification protocol language governing title and nutrition checks

**Governing requirement / approved amendment**

For this rollout, untagged means main and main-dish nutrition requirements apply. `[non-main]` is the sole role classification and is exempt from those main-dish nutrition targets. Destination defects use the fixed diagnostic tags. `[main]`, old finer-grained role tags, and `[blockers unreviewed]` are rejected.

**Required change**

- Land an explicit versioned protocol amendment for `[non-main]` and nutrition scope before generating schemas or activating the tool.
- Keep title validation minimal: `[non-main]`, the two destination-defect tokens, and other brackets only where the governing task/protocol explicitly supports them.
- Do not invent a broader role taxonomy in this version.

**Human input:** none; this policy is settled.

## C-10 — Destination parsing, movement eligibility, and task placement

**Covers:** M-24, M-26, and M-27.

**Mismatch**

Destination validity currently gates preparation/approval, signoff is coupled to movement, and the submission-centric workflow does not fully respect Research Queue, Verification Queue, or manually positioned tasks.

**Affected locations**

- `resolve_destination()` and section helpers in `models.py`
- destination validation in `validation.py`
- move/signoff logic in `commands.py`
- movement documentation

**Governing requirement**

A missing/invalid destination does not block Research, Verification, or `ready`; it blocks only the final move. Research moves only Research Queue → Verification Queue after the canonical handoff write. Verification moves only Verification Queue → valid Destination after signoff. Tasks in Research Queue at signoff, or outside both queues as manual overrides, are not moved.

**Required change**

- Separate destination parsing from move eligibility.
- Never invent or silently repair destination data.
- Keep defects visible in title and field while allowing `ready`.
- Record signoff independently from move completion/failure.
- At Planning/start, distinguish new tasks, tasks in queues, and tasks manually positioned outside queues.
- Preserve live manual placement over a stored destination.

**Human input:** none.

## C-11 — Persistence, migration, activation, and recovery

**Covers:** M-33 and M-39, plus migration consequences across the register.

**Mismatch**

The database hard-codes obsolete families, levels, `baseline_verification_line`, one verifier family, one final write, and one generic Human state. It also lacks the settled task `Schema version` compatibility gate and explicit migration behaviour. Activation assumes a mixed-authority beta and bulk corpus normalization rather than current-schema validation and per-task reconciliation.

**Affected locations**

- `bin/dish_tool/database.py`
- `bin/dish_tool/recovery.py`
- `bin/dish_tool/admin.py`
- `bin/dish-tool-activation.md`
- migration assumptions in `bin/dish-tool-imp.md`

**Governing requirement**

Task-native state and exact-content Verification cycles are authoritative. Legacy readiness/provenance cannot be inferred.

**Required change**

**Protocol-required persistence capabilities**

- Keep tool workflow state separate from the authoritative live task state.
- Track exact-content identity, history, post-write rereads, and drift.
- Track the task's `Schema version`; all seven authoritative fields; original researcher; latest material editor; verifier run/attestation; Verification cycles and their exact Verification-protocol identities; Evidence/Human Review and resume state; recoverable write/move completion; and movement state independent of signoff.

**Recommended implementation**

- Use immutable content-version records plus separate Verification-cycle, stop-state, operation, and movement records. Other schemas are acceptable if they preserve the same history, invariants, recovery, and future migration path.

Quarantine all nonterminal legacy records and reconcile individually. Keep the old project/database untouched as backup until accepted. Tasks whose `Schema version` differs from `honest/DISH_VERSION` must be refused for normal operations and migrated explicitly. Activation must target the current protocol/schema baseline in `honest`, including the approved `[non-main]` amendment, rather than silently treating the original archive or old manifests as current.

**Human input:** none.

## C-12 — Documentation, tests, reports, and administration certify the obsolete lifecycle

**Covers:** M-35 to M-40 except the persistence portions already covered above.

**Mismatch**

Repository instructions, operator docs, the old implementation plan, activation guide, relay, fixtures, tests, SQL reports, and admin commands all describe or enforce obsolete fields, routing, correction states, write sequencing, and escalation meaning. Passing the current tests would certify the wrong system.

**Affected locations**

- `ai-tools(21)/CLAUDE.md`
- `bin/dish-tool.md`
- `bin/dish-tool-imp.md`
- `bin/dish-tool-activation.md`
- `bin/dish-chatgpt-relay.md`
- `bin/tests/fixtures/protocol-release/*`
- dish-tool tests
- `bin/dish-reports.sql`
- `bin/dish-admin` and admin modules

**Governing requirement**

All requirements summarized in C-01 through C-11.

**Required change**

- Replace rather than patch the obsolete fixture release and lifecycle tests.
- Add conformance tests for state combinations, stage isolation, exact-content binding, actor independence, Small/Large routes, Evidence/Human Review, two-pass reset, destination-nonblocking readiness, main/non-main nutrition scope, queue/manual movement, external drift, and cooking readiness.
- Rebuild reports around live task Status and Verification-cycle facts.
- Make admin actions perform protocol-valid, version-bound transitions rather than raw local-state mutations.
- Rewrite documentation only after behaviour is implemented, so it does not claim safeguards that are not live.
- Mark the old `dish-tool-imp.md` superseded when the implementation-ready replacement is accepted.

**Human input:** none.


## C-13 — Agent activation boundaries and tool-result handling

**New mismatch identified during review.**

**Mismatch**

The current activation guide is a cutover checklist, not an agent operating contract. The Planning, Research, and Verification protocols do not define mandatory tool-check boundaries, and no single document owns command syntax, deterministic-result meaning, rerun behaviour, or the distinction between a dish problem and a tool failure. This permits a tool pass to be mistaken for semantic approval, or a tool error to be misrouted as Evidence or Human Review.

**Affected locations**

- `bin/dish-tool-activation.md`
- command/result handling in `bin/dish`, `bin/dish_tool/cli.py`, `results.py`, and `errors.py`
- the Planning, Research, full Verification, and compact Verification protocols
- duplicated or conflicting operational guidance in `bin/dish-tool.md`, `bin/dish-tool-imp.md`, and the relay

**Governing requirement / approved rollout decision**

The tool provides deterministic validation and workflow operations; it does not replace agent judgment or the governing protocol. The governing protocol wins over a schema or tool result. Tool execution failures are tooling failures, not dish Evidence or Human Review states.

**Required change**

- Replace the activation runbook with one canonical operating contract. Its opening must distinguish the two working locations: Asana holds the authoritative title, body, workflow state, provenance, and cooking instructions, but agents access all of it only through the dish tool; the `ai-tools` checkout provides that mediated interface plus deterministic validation and workflow operations. Neither the tool nor its database replaces agent judgment or the governing protocol.
- State the tool location, bundled-interpreter invocation (normally `ai-tools/bin/.venv/bin/python3` or its checkout-relative equivalent), exact supported commands per phase, required identifiers/arguments, structured output, exit codes, rerun rules, and troubleshooting. Document actual implemented syntax, not proposed commands.
- Define one response contract:
  - **Pass:** deterministic conformance only; continue the phase's semantic work.
  - **Agent-correctable failure:** correct the task, update provenance or `Material changes` where required, write/re-read the exact live task, and rerun.
  - **Possible Evidence or Human Review:** enter those states only when the underlying protocol issue independently meets their definitions; a tool message alone is insufficient. Small, Large, Evidence, and Human Review routing remains a protocol/agent judgment.
  - **Execution error or ambiguous result:** preserve task state and content; report command, task/content version, error, and diagnostics as a tooling failure.
  - **Tool/protocol disagreement:** fail closed, preserve the live task, stop the affected transition, and report the conformance defect. The protocol wins.
- Add concise mandatory hooks to the agent protocols while leaving mechanics centralized:
  - **Planning:** run the Planning check before handing a task to Research; correct and rerun agent-owned failures. A pass does not establish substantive plan quality.
  - **Research:** run a pre-handoff check against the exact live candidate; correct, record material edits, write and re-read the complete task, then run the handoff transition/check against the resulting `pending-verification` task before any queue move. A pass is necessary but does not replace Research or self-review.
  - **Verification:** run before semantic review, after every Small or Large correction, and immediately before signing the exact final task. Small may sign in the same pass after recheck; Large remains `pending-verification` for another fresh verifier, who reruns the tool. Every post-signoff body edit opens a new cycle. Apply the same boundary rules to full and compact instructions while both remain active.
- Keep commands, environment setup, arguments, schemas, output fields, exit codes, and operational troubleshooting out of the protocols. They belong only in the activation document.

**Human input:** none. The exact command names and exit codes are implementation outputs and must be documented after they exist.

---

# Acceptance criteria for the revised tool

A compatible implementation must satisfy all of the following:

1. The live Asana task is the content authority, and every agent read, write, correction, check-in, signoff, and move is mediated by the dish tool or its shared service; stale content is detected before mutation or signoff.
2. The six-field Planning brief, canonical task structure, separate task-body `Schema version` metadata field, and seven-field state block parse and render deterministically.
3. Tool-internal operation state never implies protocol readiness.
4. Planning, Research, and Verification receive only their own protocol text.
5. `honest/DISH_VERSION`, the schema in `honest`, and `ai-tools` capability agree exactly before the tool runs; `bin/git-commit` flags governed protocol/schema changes whose required version bump is missing; each task body contains a separate canonical `Schema version` metadata field, while every entry into `pending-verification` separately records the then-current exact Verification protocol.
6. Verification signoff is by a fresh independent ChatGPT run bound to the exact candidate.
7. `Researched by`, `Self-verified`, and `Verified by` obey their distinct provenance semantics.
8. Small, Large, post-signoff, Evidence, Human Review, and two-pass workflows follow the governing routes above.
9. Material support, source disagreements, Research-basis classification, and Material changes are preserved in the task.
10. Untagged tasks are main; `[non-main]` is the only role tag; destination defects use the two approved markers.
11. Missing/invalid destination allows Research, Verification, and `ready`, but blocks final movement.
12. Research handoff writes and confirms the canonical task before RQ → VQ. Signoff and VQ → Destination are separate operations.
13. Tasks in RQ at signoff or manually positioned outside both queues are not auto-moved.
14. Every transition rewrites and validates the complete relevant state coherently, then re-reads the live task.
15. Cooking readiness is based only on the valid exact live task at `Status: ready`.
16. Legacy records are quarantined; no readiness or provenance is inferred. Older-schema tasks are refused with `migration required`, and a migration updates `Schema version` only after the transformed live task is written, reread, and validated.
17. Planning, Research, and Verification run deterministic checks at their defined boundaries, but every semantic duty and routing decision remains agent/protocol-owned.
18. One activation document owns actual commands, bundled-environment invocation, arguments, schemas, structured outputs, exit codes, rerun rules, and troubleshooting; protocols do not duplicate those mechanics.
19. Tool results distinguish pass, agent-correctable finding, possible protocol stop state, execution error, and tool/protocol disagreement without converting tooling failures into dish blockers.
20. Local V1 testing is restricted to one active agent at a time. Multi-agent live use is blocked until one shared laptop-hosted dish service owns the lock/lease, shared submission state, and all Asana access for both GPT Actions and CLI clients. GPT Action exposure/authentication is settled: Tailscale Funnel plus a dedicated scoped bearer token and trimmed OpenAPI surface, matching `plant-monitoring`'s Assistant API pattern.

# Recommended implementation order

1. **Safety stop:** mark the current tool incompatible and prevent accidental claims that its approval/`ready` states satisfy the protocols.
2. **Protocol/schema baseline in `honest`:** land the required protocol amendments, add `DISH_VERSION`, move the authoritative machine schema beside the protocols, define the first explicit task-schema migration, and update `bin/git-commit` to block or question missing protocol/schema version bumps when governed files change.
3. **Compatibility and conformance layer:** replace task-pinned bundle freezing with exact `PROTOCOL_VERSION`/`SCHEMA_VERSION` support checks; load the schema from `honest`; then build clause-linked parsers, renderers, legal-state tests, and canonical result/exit categories.
4. **Persistence redesign:** add task `Schema version`, exact-content identity/history, live baselines, Verification cycles, actor identity, Evidence/Human Review, and independent movement tracking; immutable version records are the recommended design.
5. **Asana transaction layer and local test mode:** make the dish tool the sole supported agent interface to Asana; implement lock-serialized full-state writes, post-write rereads, exact-content drift detection, and handoff-before-move ordering. Permit controlled single-agent local testing without a shared service, but state that SQLite in copied repositories does not provide cross-agent locking.
6. **Stage and provenance routing:** remove family routing and enforce stage isolation plus exact provenance semantics.
7. **Correction and stop states:** implement Small, Large, post-signoff reset, Evidence, Human Review, and two-pass behaviour.
8. **Destination and movement:** implement nonblocking diagnostics and the exact queue/manual-placement rules.
9. **Migration:** implement explicit, preferably scripted migration; refuse older-schema tasks during normal commands; migrate a task by writing, rereading, and validating before changing `Schema version`; quarantine and individually reconcile legacy tasks; retain old records/project as backup.
10. **Replace tests and operational surfaces:** rebuild fixtures, tests, reports, admin flows, and relay. Add boundary tests for Planning, Research, and Verification plus every result-contract category.
11. **Agent integration documentation:** update Planning, Research, full Verification, and compact Verification with concise mandatory hooks; replace `dish-tool-activation.md` with the actual command/environment/result contract; remove duplicated mechanics elsewhere.
12. **Shared-service live mode:** add a small laptop-hosted dish API. Move lock/lease ownership, shared submission state, and all Asana access behind it; make GPT Actions and the CLI clients of the same API. Expose it via Tailscale Funnel; give the GPT Action its own dedicated bearer token scoped only to its endpoints and a trimmed OpenAPI document limited to that surface, matching `plant-monitoring`'s Assistant API pattern.
13. **Controlled activation:** sandbox Asana and run single-agent tests first; test drift/recovery and agent reruns; migrate a small reviewed cohort; permit proper multi-agent go-live only after the shared-service gate passes and no mixed old/new Asana path remains.

# Remaining decisions

The settled release model remains:

- `honest` owns current protocols, machine schema, migrations, and `DISH_VERSION`;
- `ai-tools` is the generic engine and runs only against the exact supported protocol/schema versions;
- `bin/git-commit` guards against governed-file changes being committed without the required version bump;
- active tasks follow current protocol prose and store only `Schema version` for compatibility;
- older-schema tasks require explicit migration;
- `Verification protocol release` remains a separate per-cycle audit field.

One decision is deliberately deferred:

1. **Breaking protocol changes during an open submission.** Until a real case requires a policy, restrict changes made while submissions are open to backward-compatible/minor changes. A future breaking change must define whether open submissions restart, migrate, or follow another explicit route before that change is used.

GPT Action connectivity is settled, not deferred: Tailscale Funnel plus a dedicated scoped bearer token and trimmed OpenAPI surface, matching `plant-monitoring`'s Assistant API pattern (`~/plant-monitoring/docs/internals.md`).

Local single-agent testing can begin before the remaining deferred decision is needed. Low-risk serialization and file-layout details may be revised later without changing the settled invariants.

# Test verification

The tests were rerun against the analysed `ai-tools(21)` source using the Python executable and bundled dependencies from `ai-tools(24)`. The extracted virtual environment had been created under a different absolute path, so its bundled `site-packages` directory was supplied explicitly to the environment's interpreter.

Results:

- dish-focused suite: **183 passed**;
- complete `bin` suite: **300 passed**;
- repository suite covering `bin/tests` and `hooks/tests`: **398 passed**.

There were no collection errors or test failures. These results show that the existing implementation is internally consistent with its current test suite; they do not reduce the protocol mismatches in this report, because the tests themselves encode the obsolete lifecycle in several areas and must be replaced or extended as part of the update.
