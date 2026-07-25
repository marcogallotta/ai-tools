# Dish tool — protocol-compatibility update implementation plan

**Companion to:** `dish-tool-update.md`

**Scope:** update the existing implemented dish tool so it conforms to the current dish protocols and the settled rollout decisions in `dish-tool-update.md`, while preserving the original guarded-submission purpose of the tool.

This is an implementation plan, not a new design discussion. Do not reopen settled behaviour unless implementation proves an actual contradiction. Where this plan leaves a low-level choice open, choose the smallest implementation that preserves the stated invariant and record the choice in the code and test name.

The existing command lifecycle remains because it provides a useful guarded envelope around one task change:

```text
start → prepare → approve/reject → submit
```

Its meaning changes:

- `start` claims one task operation, reads the exact live baseline through the tool, and returns the current stage instructions.
- `prepare` validates and writes the candidate to the live Asana task, rereads it, and records the exact resulting content version. It is no longer a local-only validation step.
- `approve` signs the exact live version after protocol-required Verification. It does not perform destination movement.
- `reject` applies only non-signing protocol routes against the exact live version: Large correction, Evidence, Human Review, or another explicit stop route. A corrected Small is rechecked and signed through `approve` in the same Verification pass.
- `submit` performs only the remaining post-signoff movement or retryable completion work. It must never repeat an already-confirmed content write.

For protocol-managed tasks, agents do not access Asana directly: every read, write, correction, check-in, signoff, and move goes through `dish` in local test mode or through the shared dish service in live mode. The registry defines which tasks those are; unmanaged work such as the Pantry and Fermentation projects stays outside this rule.

Title and body are both guarded content: a generic rename registers as drift exactly as a body edit does, since title is part of the signed exact content and renaming is otherwise the cheapest way to invalidate a signature. Ordinary non-content operations — scheduling or clearing `due_on`, marking a task complete after cooking, other non-body fields — remain available outside the tool and never register as drift.

Cooking is the one deliberate exception in this rollout. Cooking agents write cook-log entries — Asana comments today, a cook-log record once the backend changes — and never touch the task body, so a cook log can never invalidate exact-content signoff. A cooking-agent body edit takes the task out of guarded state and requires re-verification before further protocol work. A Marco-granted override is a cook-log entry naming exactly what was waived, not a tool bypass. Routing cooking through the tool is deferred; see `dish-tool-future.md`.

Planning reads Asana cooking history directly through the general `asana` CLI to populate the brief's `Priors`. That is not a breach of the rule above: it reads completed, unmanaged history rather than governed task content.

## Authority and non-negotiable invariants

Implementation must preserve all of these:

1. `honest` owns the current protocol prose, machine-readable schema, migrations, and `DISH_VERSION`.
2. `ai-tools` is a generic engine. It runs only when it supports the exact current `PROTOCOL_VERSION` and `SCHEMA_VERSION` declared by `honest`.
3. A task stores only its `Schema version`; it is not pinned to an old general protocol release.
4. Every entry into `pending-verification` records the exact Verification protocol used for that cycle.
5. The exact live Asana title and body are authoritative after every tool-mediated write.
6. Any stale baseline, out-of-band edit, or ambiguous write outcome fails closed.
7. Tool-local operation state never substitutes for the task’s seven authoritative state fields.
8. Planning, Research, and Verification receive only their own protocol text.
9. Verification is performed by a fresh independent ChatGPT run against the exact live content.
10. Small, Large, Evidence, Human Review, post-signoff reset, and two-pass behaviour follow the protocols.
11. Signoff and destination movement are separate recoverable operations.
12. Local V1 testing is single-agent only. Multi-agent live use requires one shared laptop-hosted service owning the lock, shared operation state, and Asana access. GPT Action network exposure and authentication is settled — see `dish-tool-update.md` C-02's V1 staging decision for the architecture.

## Implementation sequence

Land Steps 1–12 as independently testable commits. A later step may depend on earlier database or schema migrations, but each commit must leave the repository testable. Do not activate multi-agent use before Step 11’s shared-service gate passes.

The existing passing suite is a regression baseline, not a conformance certificate. Preserve unrelated `asana`, hook, and CLI behaviour while replacing dish tests that encode obsolete requirements.

---

## Step 1 — `honest` release assets and generic compatibility resolver

Replace the wrapper-owned task-pinned release bundle with the settled current-release model.

### Files in `honest`

All of the following land only on `~/honest-pantry-dish-rollout` (the rollout branch/worktree),
never on production `~/honest-pantry` — `dish-docs-design.md` forbids a mixed production state, and
production has no `DISH_VERSION` by design until deploy.

- new uppercase `DISH_VERSION`
- machine-readable task schema file or files
- first schema migration definition/script
- versioned protocol amendments already approved in `dish-tool-update.md`

### Files in `ai-tools`

- `bin/git-commit`
- `bin/dish_tool/constants.py`
- `bin/dish_tool/releases.py`
- `bin/dish_tool/models.py`
- `bin/dish_tool/validation.py`
- resolver tests and fixtures

### Required `DISH_VERSION` shape

```text
PROTOCOL_VERSION=<version>
SCHEMA_VERSION=<version>
```

Use a deliberately simple parser:

- exactly one value for each key;
- no unknown keys in V1;
- non-empty values;
- deterministic error messages for missing, duplicate, or malformed lines.

### Work

- Remove `protocol_release` as the general task/submission release identity.
- Remove task-lifetime freezing of Planning, Research, Verification, and manifests at `dish start`.
- There are two `honest` checkouts on disk — `~/honest-pantry` (production, still running the retired
  `dish-protocol.md`, no `DISH_VERSION`) and `~/honest-pantry-dish-rollout` (the frozen three-way
  protocols and the branch/worktree `DISH_VERSION` and schema actually land on). The resolver must not
  guess between them: require an explicit configured path (config file or required env var, not a
  default-if-unset), refuse to start without one, and fail closed with a distinct
  `DISH_VERSION missing` error — never silently falling back to loading protocol text without a
  version check — so a resolver mispointed at `~/honest-pantry` fails loudly instead of loading the
  wrong protocol generation unversioned.
  - Load, once bound to that path:
    - `DISH_VERSION`;
    - the current machine schema;
    - the stage-specific protocol requested by the command;
    - schema migration metadata when migration is invoked.
- Add an `ai-tools` capability declaration for the exact supported `PROTOCOL_VERSION` and `SCHEMA_VERSION`.
- Retrieve the verification protocol text recorded in `Verification protocol release` — by Git commit, or by the recorded hash and read time without Git. The verification protocol requires verifying against that exact text and stopping if it cannot be recovered, so retrieval must work independently of the current-version compatibility gate. Fail closed with a distinct error when the recorded release is unreachable.
- Update `bin/git-commit` to inspect the staged diff for governed protocol, schema, and migration files:
  - a governed protocol change requires a staged `PROTOCOL_VERSION` bump;
  - a schema or migration change requires staged `SCHEMA_VERSION` and `PROTOCOL_VERSION` bumps;
  - if the required bump is absent, stop or require an explicit `does this need a bump?` confirmation before commit;
  - do not silently auto-bump in V1. Auto-bumping may be added later only where the correct bump is deterministic and explicitly accepted.
- Fail closed if either declared version differs from what the engine supports.
- Validate that the schema declares the same `SCHEMA_VERSION` as `DISH_VERSION`.
- Keep prose/schema disagreement fail-closed. The resolver does not decide semantic precedence; the protocol remains authoritative and the mismatch is reported as a conformance defect.
- Preserve useful manifest-driven validation. The schema remains external configuration in `honest`; do not duplicate it as the authority in Python.
- Add clause/source identifiers to schema rules so diagnostics can identify the governing protocol requirement.

### Tests

- valid `DISH_VERSION` and schema load successfully;
- missing, duplicate, malformed, or unknown version keys fail;
- an unconfigured `honest` path refuses to start rather than defaulting;
- historical verification text loads from its recorded commit while the current-version gate is unchanged, and an unreachable commit fails closed with its own error;
- a configured path with no `DISH_VERSION` (e.g. pointed at production `~/honest-pantry`) fails
  closed with the distinct `DISH_VERSION missing` error, not a generic load failure;
- unsupported protocol version fails;
- unsupported schema version fails;
- schema-declared version mismatch fails;
- missing or malformed schema fails;
- the engine loads Planning, Research, Verification, or Cooking text only when requested;
- no command stores or reuses a whole protocol bundle as task-lifetime authority;
- staged governed protocol changes without a `PROTOCOL_VERSION` bump are blocked or explicitly questioned;
- staged schema/migration changes without both required bumps are blocked or explicitly questioned;
- ordinary code-only changes do not require a protocol/schema bump.

### Deferred breaking-change policy

Do not build an automatic restart/rebind policy for open submissions in V1 — deferred per `dish-tool-update.md`'s Remaining decisions. Operationally, restrict in-flight protocol changes to backward-compatible/minor ones until that policy exists.

### Completion gate

The tool runs only against one exact supported current protocol/schema pair, loads the authoritative schema from `honest`, and the commit helper prevents silent governed-file changes without the corresponding version bump.

---

## Step 2 — canonical task parser, renderer, and schema migration primitives

Implement the current task structure before changing lifecycle commands.

### Files

- `bin/dish_tool/validation.py`
- `bin/dish_tool/models.py`
- optional new modules:
  - `bin/dish_tool/task_document.py`
  - `bin/dish_tool/migrations.py`
- schema files in `honest`
- parser/render/migration tests

### Work

Implement deterministic parse/render support for:

- the eight-field Planning brief;
- canonical complete-task top-level sections;
- permitted lower-level subheadings;
- fixed Process Record labels;
- the seven-field task state block:
  - `Status`;
  - `Status detail`;
  - `Resume status`;
  - `Verification protocol release`;
  - `Researched by`;
  - `Verified by`;
  - `Self-verified`;
- separate canonical task-body metadata field `Schema version: <version>`; this is not an eighth member of the seven-field state block, an Asana custom field, or a subtask;
- `Destination section: <section name> — <section gid>`;
- title grammar:
  - untagged means main;
  - `[non-main]` is the only role tag;
  - `[destination missing]` and `[destination invalid]` are canonical destination defect markers;
  - other protocol-approved blocker/dependency text remains governed by schema rules;
- approved Human, source, and Material changes formats;
- explicit Research-basis classification.

Validation must distinguish:

- syntax/structure errors;
- illegal field combinations;
- agent-correctable deterministic findings;
- possible semantic Evidence/Human Review issues that the tool cannot decide;
- schema-version mismatch.

Add migration primitives that:

1. parse a task under its declared old schema;
2. transform to the next schema version;
3. render the complete candidate;
4. validate under the target schema;
5. return the transformed content without yet claiming success.

Do not infer `ready`, provenance, Human decisions, or unsupported semantic facts.

### Tests

- parse/render round trips for Planning and complete tasks;
- exact-once state fields;
- legal and illegal status combinations;
- lower-level headings allowed while extra top-level sections/process labels fail;
- title grammar and destination markers;
- main/non-main nutrition-scope interpretation;
- approved provenance formats;
- Research-basis remains explicit;
- migration transforms only supported old structures;
- ambiguous legacy content is quarantined rather than guessed;
- target schema version is not written by the migration primitive itself.

### Completion gate

Current task documents can be parsed, rendered, validated, and transformed independently of Asana and command lifecycle code.

---

## Step 3 — persistence redesign for operations, content identity, and Verification cycles

Replace obsolete database concepts while keeping the database an operation/audit store, not the content authority.

### Files

- `bin/dish_tool/database.py`
- `bin/dish_tool/models.py`
- `bin/dish_tool/recovery.py`
- database migration tests

### Required capabilities

Persist enough information to recover and audit:

- one open tool operation/submission per task within one shared state store;
- task GID and operation kind;
- editor/researcher/verifier identities and run/session ID or independence attestation;
- expected live title/body identity at each operation boundary;
- last confirmed live title/body identity, persisted at **task** scope and surviving the end of the operation that recorded it. Operation-scoped identity alone cannot detect an edit made while no operation is open — which is the whole exposure, since a signed `ready` task that has been submitted and moved has no open operation and, now that cooking writes only cook-log entries, may never have one again;
- task `Schema version`;
- Verification-cycle number and exact `Verification protocol release`;
- correction class and outcome;
- Evidence/Human Review route and resume state;
- content-write, signoff, and movement completion independently;
- write-attempt identity and uncertain-outcome recovery;
- audit events for every command result.

### Recommended schema

Use separate records for:

- `operations` or revised `submissions`;
- immutable or append-only `content_versions` containing hashes and minimal snapshots needed for audit/recovery;
- `verification_cycles`;
- `write_attempts`;
- `movement_attempts`;
- `audit_events`.

An equivalent design is acceptable if it enforces the same invariants. Do not persist a local candidate as the authority.

### Content identity

- Compute a stable identity over the exact title and notes returned by Asana.
- Normalize only proven transport differences, initially CRLF/LF.
- Store raw returned title/notes or a recoverable snapshot where needed to diagnose drift and uncertain writes.
- Every mutating command takes an expected identity and fails if live content differs.

### Legacy database handling

- Preserve the old database as backup.
- Quarantine nonterminal old submissions; do not convert their local `ready`, verifier family, or rejection count into protocol state.
- Add a read-only inspection path for legacy rows.
- Do not claim SQLite coordinates copied repositories. Local mode is explicitly single-agent.

### Tests

- database migrations are idempotent;
- open-operation uniqueness works within one database;
- content identities are stable across CRLF/LF only;
- stale expected identities fail atomically;
- write, signoff, and move markers are independent;
- Verification cycles retain exact release and actor identity;
- legacy nonterminal rows are quarantined;
- no legacy row implies task readiness;
- every transition emits an audit event.

### Completion gate

The local database can safely support one-agent test mode and contains the records needed by the later shared service without becoming the task-content authority.

---

## Step 4 — backend transaction layer and exact live-task operations

Make every Asana interaction exact, guarded, and reread-confirmed.

Drift/external-edit detection (steps 2–3 below) is V1-mandatory here, overriding
`dish-tool-future.md`'s earlier "not the first post-v1 release" deferral: the frozen protocols'
exact-content signoff makes it load-bearing now, not optional. Automated *recovery* from an
uncertain outcome remains deferred per that doc; only detection is pulled forward.

### Protocol-managed task registry

The registry (live section-GID resolution for `Sourcing`/`Reference`, fail-closed-to-managed on an
unresolvable section) survives this update unchanged in behaviour — see `dish-tool.md`'s Protocol-managed
task registry section. What changes is only where its checks sit: resolution is schema/version-aware,
running against the current `honest` `DISH_VERSION` rather than a task-pinned bundle. The registry is
now purely the tool's own scoping concept; the generic Asana CLI is not modified to consult it, and
drift detection catches writes made outside the guarded path.

### Files

- `bin/dish_tool/backend.py`
- `bin/dish_tool/commands.py`
- optional new `bin/dish_tool/task_store.py`
- backend fakes and transaction tests

### Work

Add backend operations for:

- complete task read, including title, notes, memberships, and update/version metadata available from Asana;
- complete title-and-notes write;
- state-only complete-task rewrite;
- move to section;
- reread after every mutation.

Every write operation must:

1. read the live task through the tool;
2. compare it to the operation’s expected content identity and expected placement;
3. refuse on drift;
4. make one bounded backend mutation;
5. classify clear non-application, confirmed application, and ambiguous outcome conservatively;
6. reread the task;
7. confirm exact expected title/body and state;
8. record the new content identity only after confirmation.

Do not allow agent code to call generic Asana mutation paths for managed tasks. In local test mode this is a workflow rule only, enforced after the fact by drift detection; in live mode Step 11 makes the service the only credentialed path.

### Tests

- stale live content blocks before mutation;
- exact expected write succeeds and rereads;
- post-write mismatch fails closed;
- timeout/5xx/lost response becomes uncertain unless reread proves the outcome;
- confirmed non-application is retryable;
- confirmed application is idempotently recorded;
- movement never rewrites content;
- content retry never repeats a confirmed write;
- manual/out-of-band edit is detected as drift;
- a generic body edit made between two operations — with no operation open — is detected at the next `dish start` and fails closed;
- a generic rename of a signed task is detected as drift;
- a `due_on` change or completion toggle is not.

### Completion gate

The tool can prove which exact live task content existed before and after every mutation.

**This step owns drift detection end to end.** It is the only remaining protection against a write
made outside the tool — the generic-CLI guard was dropped precisely because drift detection replaces
it — so it is called out here rather than left implicit across Steps 3, 4 and 5. Do not close this
step until a title or body edit is detected in all three cases: during an open operation, between
two operations with none open, and on a signed `ready` task that has already been submitted and
moved.

---

## Step 5 — `dish read`, `dish inspect`, `dish start`, and explicit migration

Rebuild the entry commands around current versions and exact live content.

### Files

- `bin/dish_tool/cli.py`
- `bin/dish_tool/commands.py`
- `bin/dish_tool/admin_cli.py` or a new migration command surface
- command tests

### `dish create`

The only V1 path from nothing to a bare task — `dish planning`/`dish start --kind planning` is the
only path onward from there, so generic `create_task` with notes cannot bypass either step.

- create one bare task in the Cooking project's Research Queue, no notes write;
- return the task GID through the common JSON envelope;
- a clear API failure returns `BACKEND_REJECTED`; an ambiguous outcome returns `BACKEND_UNCERTAIN`
  rather than an automatic retry that risks a duplicate task;
- stamp the created task's `Schema version` to the current `honest` `SCHEMA_VERSION` at creation time.

### `dish sections`

Planning must record a `Destination section` name and gid and cannot invent one.

- list the Cooking project's sections with names and gids, read-only;
- scoped to Cooking by construction — it must not be usable to query Pantry or Fermentation, which
  the planning protocol calls out explicitly;
- no dedupe search command is provided: Planning assumes the dish does not already exist.

### `dish read`

Return through the common JSON envelope:

- exact live title and notes;
- parsed canonical fields;
- task `Schema version`;
- current content identity, and its drift status against the stored task-scoped identity;
- project/section placement;
- compatibility and validation diagnostics.

Reads are permitted for older-schema tasks, but the result must state `migration required` for normal mutation.

### `dish inspect`

Return:

- current operation/submission state;
- exact expected and confirmed content identities;
- authoritative task state fields;
- actor/provenance summary;
- Verification-cycle summary;
- write/signoff/move completion;
- legal next tool actions;
- compatibility and drift status.

Do not return a frozen whole-protocol bundle.

### `dish start`

- verify exact `honest` compatibility;
- read the live task through the tool;
- reject old task schema with `migration required`;
- validate the task’s starting structure and state for the requested operation kind;
- compare exact live content identity against the stored task-scoped identity, and fail closed on a mismatch: an edit made outside the tool voids any signoff and the task must be re-verified before further protocol work. Only then record the new identity and placement;
- claim the task in the local operation store;
- return only the stage-specific current protocol text and schema diagnostics needed by that agent;
- record the constructor/editor identity.

Preserve the original lock/claim purpose. In local mode the claim is valid only under the documented one-agent limitation.

### Explicit migration command

Add `dish-admin migrate`, a Marco-only explicit command, that:

1. reads the older-schema live task;
2. verifies a supported migration path;
3. transforms and validates the complete target candidate;
4. writes the complete live task with the old `Schema version` still present or with a transaction-safe pending marker if required by the chosen representation;
5. rereads and validates the exact transformed content;
6. only then writes/commits the new `Schema version` as part of the confirmed final content;
7. rereads once more and reports success.

If any stage fails, the task must not be reported as migrated. Prefer a single complete final write where the backend and transformation allow it; the invariant is that the new schema version is never left on unvalidated content.

### Tests

- read/inspect expose current exact state;
- start rejects incompatible versions and old task schemas;
- start returns only its stage protocol;
- simultaneous local starts allow one claim within one database;
- migration-required errors are clear and non-destructive;
- successful migration updates the version only after validation;
- failed/ambiguous migration does not claim success or infer state.

### Completion gate

Agents can begin a guarded operation only from a compatible, exact live task, and old tasks have an explicit safe migration path. `dish start` refuses to open an operation on a task whose live content has drifted from its stored task-scoped identity.

---

## Step 6 — `dish prepare`: guarded live check-in and Research handoff

Change `prepare` from local candidate acceptance into the controlled live-task write/check-in.

### Files

- `bin/dish_tool/commands.py`
- `bin/dish_tool/validation.py`
- CLI argument definitions
- stage-specific tests

### Common behaviour

`prepare` must:

- require the recorded editor/researcher identity;
- load an ephemeral candidate input supplied to the tool;
- validate it against the current schema and the correct current stage protocol;
- compare the live task to the baseline captured by `start` or the latest confirmed version;
- record required `Material changes` for body edits, including the editor's material/non-material classification;
- write the complete candidate to Asana;
- reread and validate the exact live result;
- record the new content identity;
- return the exact live task to the agent through the tool.

The candidate file is an input only and may be deleted after the operation. It is never the Verification object.

### Planning handoff

- validate the eight-field Planning brief;
- preserve locks/exemptions explicitly;
- write and reread the live Planning task;
- leave it in the correct Research Queue state for Research pickup;
- do not claim substantive plan quality from a tool pass.

### Research handoff

Before RQ → VQ:

1. validate the complete candidate semantically-required structure;
2. set the complete seven-field block to a legal `pending-verification` state;
3. freeze and write the exact current Verification protocol identity for the new cycle;
4. write the complete live task;
5. reread and validate it;
6. run the deterministic handoff check against that exact live result;
7. move RQ → VQ only after confirmation;
8. reread placement and content after the move.

Destination defects do not block this handoff.

### Change preparation

- validate the requested change class and the current task state;
- preserve Planning locks and exemptions unless the protocol-authorized route changes them;
- never silently overwrite Human decisions;
- establish a new Verification cycle whenever the resulting content enters `pending-verification`.

### Tests

- local-only validation no longer counts as prepare success;
- stale baseline blocks before write;
- valid candidate is written and reread exactly;
- Planning handoff preserves required fields;
- Research writes `pending-verification` before moving;
- move is not attempted if write/reread/check fails;
- Verification release is current and cycle-specific;
- destination missing/invalid still permits VQ handoff;
- a body edit recorded as material invalidates prior signoff; one recorded as non-material records a new content version and leaves `Verified by` intact; both record Material changes.

### Completion gate

Every prepared candidate is already the exact confirmed live Asana task before another agent may act on it.

---

## Step 7 — Verification read, identity, and `approve`

Implement independent Verification against the exact live task.

### Files

- `bin/dish_tool/commands.py`
- actor identity helpers in `models.py`
- Verification tests

### Verification start/read

When a verifier starts or reads a pending candidate:

- require `Status: pending-verification` and a complete legal state block;
- verify the stored Verification protocol identity is valid for that cycle;
- return the exact live candidate and the exact frozen Verification protocol text for that cycle;
- record verifier run/session ID when available, otherwise an independence attestation;
- reject a verifier who constructed or materially edited the candidate;
- do not use opposite model-family routing.

### `approve`

`approve` must:

1. reread the exact live candidate;
2. compare with the verifier’s reviewed content identity;
3. rerun deterministic validation immediately before signoff;
4. require completed semantic self-review/provenance inputs from the verifier;
5. rewrite the complete state block to `ready` with valid `Verified by` and `Self-verified` semantics;
6. write and reread the exact live task;
7. record signoff against the resulting content identity;
8. leave movement incomplete for `submit`, but return `submit` as the sole `allowed_actions` entry so
   the result itself obliges the verifier to run it in the same pass rather than leaving a signed
   task unmoved in Verification Queue.

A tool pass alone cannot authorize approval; the command requires the verifier’s explicit protocol result.

### Tests

- constructor/material editor cannot verify;
- platform ID and attestation routes work;
- opposite-family assumptions are absent;
- stale candidate blocks approval;
- approval signs only the reread exact content;
- `ready` cannot be written without complete provenance and self-review;
- approval never moves the task;
- post-approval body edit recorded as material invalidates the stored signoff on the next operation; a non-material one does not.

### Completion gate

A task can reach protocol `ready` only through an independent verifier signing one exact live content version.

---

## Step 8 — `reject`: Small, Large, Evidence, Human Review, and two-pass routes

Replace the old rejection-count/family logic with protocol routes.

### Files

- `bin/dish_tool/commands.py`
- `bin/dish_tool/constants.py`
- `bin/dish_tool/admin.py`
- route-specific tests

### Small correction

- verifier supplies the corrected complete candidate through the tool;
- tool validates, writes, rereads, and records the exact new version;
- verifier performs required self-review;
- tool reruns deterministic validation;
- the same verifier may sign that corrected version in the same pass;
- record the correction and resulting signoff in `Material changes`/provenance.

`dish approve` accepts an optional corrected candidate input for the Small route. When present, it performs the guarded correction write, reread, deterministic recheck, and same-pass signoff as one bounded operation. `dish reject` is reserved for Large, Evidence, Human Review, or other non-signing outcomes.

### Large correction

- verifier supplies or applies the complete correction through the tool;
- tool validates, writes, rereads, and records the exact new version;
- leave `Status: pending-verification`;
- open a new Verification pass requiring a fresh independent verifier;
- the correcting verifier cannot sign that version.

### Evidence

- enter `pending-evidence` only when the underlying issue is a material factual input Marco must supply;
- set `Resume status` correctly;
- record the specific missing evidence;
- tool execution failures never create this state.

### Human Review

- enter `pending-human-review` only for Marco’s preference, authorization, classification, route decision, or risk acceptance;
- preserve scope and reason in the approved Human format when resolved;
- resume through the recorded `Resume status`;
- tool execution failures never create this state.

### Two-pass reset

After two independent passes without a signable task, write the task-native hold — `Status:
pending-human-review`, `Resume status: pending-verification`, reason in `Status detail` — and block
agent workflow commands. The hold must live on the task, not only in tool state, so a reader outside
the tool does not see `pending-verification` and pick the task up as a fresh verifier. Only Marco's
admin action reopens it; an agent never clears the stop by recording its own reset, since the stop
exists to end repeated verification cycling. Reopening requires:

- a new Material changes entry;
- category `evidence`, `premise`, `method`, or `scope`;
- concrete before/after detail, editor, and date;
- a new hash/version alone is insufficient;
- retained prior cycle history.

Remove:

- `failed_verification_passes` as the routing authority;
- opposite-family reassignment;
- generic `awaiting_human` without protocol distinction.

### Tests

- Small correction writes, rereads, rechecks, and signs in one pass;
- Large correction cannot be signed by its editor;
- new verifier is required after Large;
- Evidence/Human states require matching underlying reasons;
- execution errors preserve task state;
- resume state works;
- two-pass hold writes the task-native state, blocks agent workflow commands, and cannot be cleared without Marco's admin reopen;
- two-pass reset requires substantive category and before/after details;
- prior Verification cycles and reasons remain auditable.

### Completion gate

Every Verification outcome follows the current protocol rather than the obsolete rejection/family state machine.

---

## Step 9 — `dish submit`: movement-only completion and recovery

Redefine `submit` so approved content is never rewritten.

### Files

- `bin/dish_tool/commands.py`
- `bin/dish_tool/recovery.py`
- `bin/dish_tool/admin.py`
- movement/recovery tests

### Behaviour

`submit` accepts a confirmed signed/`ready` operation and:

- rereads the live task;
- confirms the exact signed content identity and valid `ready` state;
- determines movement eligibility from current placement and destination;
- never modifies title or notes;
- moves only VQ → valid Destination;
- does not move a task still in RQ;
- does not move a task manually positioned outside both queues;
- succeeds without movement when destination is missing/invalid, while leaving the diagnostic visible;
- records movement independently from signoff;
- retries only an incomplete move.

If content changed after signoff, `submit` must refuse and require a new Verification cycle.

### Recovery/admin

Adapt recovery to distinguish:

- uncertain content write;
- confirmed content write but incomplete reread recording;
- confirmed signoff but incomplete movement;
- uncertain movement.

Administrative recovery must inspect the live task through the tool and never infer application solely from local state.

### Tests

- submit makes no content mutation;
- valid VQ task moves once;
- already-at-destination is idempotent;
- RQ and manual-placement tasks remain where they are;
- missing/invalid destination does not undo `ready`;
- changed content after signoff blocks movement;
- movement retry never repeats signoff or content write;
- recovery uses live reread evidence.

### Completion gate

Signoff and destination movement are independently correct and recoverable.

---

## Step 10 — activation contract, agent hooks, reports, and documentation

Update documentation only after command behaviour and result codes are stable.

Document here only what Step 11 cannot invalidate: commands, arguments, structured output, result
codes, rerun rules, protocol hooks, and reports. Leave the access path — how an agent reaches the
tool, credentials, and local versus shared-service invocation — to Step 11, which changes it. Writing
the whole activation contract here and rewriting it one step later is the failure this split avoids.

### Files

- `bin/docs/dish-tool.md`
- this implementation plan, if final command names differ
- `bin/docs/dish-tool-activation.md`
- `bin/docs/dish-chatgpt-relay.md`
- `bin/dish-reports.sql`
- relevant protocol files in `honest`
- `CLAUDE.md`/global routing only at authorized activation

### Activation document owns

- tool location;
- bundled Python invocation;
- exact command syntax;
- task/operation identifiers and arguments;
- candidate input handling;
- JSON output fields;
- result codes and process exit statuses;
- pass, agent-correctable finding, possible Evidence/Human issue, execution error, and protocol/tool disagreement handling;
- rerun rules;
- migration command and failure handling;
- operational troubleshooting.

Step 11 adds the access-path half: local test mode versus shared-service live mode, endpoint and
credential handling, and the GPT Action surface.

### Protocol hooks

Protocols should contain only:

- when the tool check is mandatory;
- what exact live task stage it applies to;
- what semantic responsibility remains with the agent;
- the rule that tool pass does not authorize substantive handoff/signoff;
- the rule that tool failures are tooling failures, not dish blockers.

Do not duplicate commands, environment setup, schemas, or exit-code tables across protocols.

### Reports

Replace obsolete reports with queries/metrics for:

- compatibility failures;
- schema migrations and failures;
- drift/stale-baseline detections;
- write outcomes and uncertain recovery;
- Verification cycles;
- Small/Large/Evidence/Human routes;
- post-signoff invalidations;
- signoff versus movement outcomes;
- tool/protocol disagreements;
- drift events, split by whether an operation was open at the time.

### Tests

- documented examples execute against CLI parser fixtures;
- every result code has activation guidance;
- protocols contain hooks but no duplicated mechanics;
- reports run against the new database schema;
- relay never tells agents to access Asana directly.

### Completion gate

An agent can follow one activation document from start through recovery without relying on undocumented command behaviour, given the local access path. The access-path half lands in Step 11.

---

## Step 11 — shared dish service and GPT Action live mode

This step is the multi-agent go-live gate. Local Steps 1–10 may be tested with one active agent at a time.

It also completes the activation document with the access-path material deferred from Step 10, so that document is written once against the final access path rather than rewritten here.

### Purpose

Provide one shared authority for:

- task lock/lease;
- operation/submission state;
- Asana credentials and backend access;
- exact-content baselines;
- audit/recovery state;
- CLI and GPT Action requests.

### Components

Create a small service layer around the same application logic used by the CLI. Avoid duplicating workflow rules in HTTP handlers.

Recommended layout:

- `bin/dish_service/` or equivalent service package;
- transport-neutral application/service methods in `dish_tool`;
- one persistent shared database on Marco’s laptop;
- laptop-hosted listener/service endpoint;
- CLI client mode, likely using the local network or Tailscale path;
- Custom GPT Action/OpenAPI surface — architecture per `dish-tool-update.md` C-02's V1 staging decision (Tailscale Funnel, dedicated scoped bearer token, trimmed OpenAPI document);
- Marco-only administrative endpoints kept separate from agent endpoints.

### Lock/lease requirements

- one active operation per task;
- atomic acquisition;
- owner/run identity;
- lease expiry or explicit recovery path;
- heartbeat/renewal for long agent work;
- stale-owner recovery by Marco/admin only or by a conservative proven-dead rule;
- no lock release before all confirmed completion markers are stored;
- no general idempotency-key requirement for ordinary full-state task writes or approval; retries must be naturally safe by detecting that the exact intended content/state is already live and returning success without duplicate provenance or repeated side effects.

Do not rely on a copied repository SQLite database for coordination.

### API behaviour

Expose bounded equivalents of:

- create/read/inspect/start;
- prepare;
- approve/reject;
- submit;
- migration;
- admin recover/discard/unblock where appropriate.

Every response uses the same result envelope as the CLI. HTTP status is transport information; workflow meaning remains in the canonical result code.

### Security and operations

- Asana token exists only on the service host;
- CLI/admin clients reach the service over the tailnet directly; the GPT Action reaches it through Tailscale Funnel with its own dedicated bearer token, never the CLI/admin credential;
- authenticate each client/action;
- log actor/run attribution without storing unnecessary secrets;
- bind request size and timeouts;
- preserve ambiguous-outcome recovery;
- provide backup/restore for shared database;
- provide a health/compatibility endpoint that checks `honest` versions and schema.

### Tests

- two clients cannot acquire the same task simultaneously;
- lease renewal and expiry are deterministic;
- retrying the same full-state write or approval does not append duplicate provenance or repeat side effects;
- CLI and Action clients receive identical workflow results;
- service restart preserves open operations and recovery state;
- unauthorized requests cannot read or mutate tasks;
- Asana credentials never leave the service;
- compatibility mismatch blocks all mutating operations;
- full concurrent correction/signoff/move scenarios preserve exact-content invariants.

### GPT Action connectivity — settled

Network exposure and authentication for the Custom GPT Action are settled — see `dish-tool-update.md` C-02's V1 staging decision for the architecture and its `plant-monitoring` precedent.

### Completion gate

All multi-agent access uses the shared service, the GPT Action connectivity path above is implemented and tested, and no agent path retains direct Asana *write* credentials for governed tasks or a separate writable operation database. Planning's read of completed cooking history through the general `asana` CLI is the one deliberate exception and stays available.

---

## Step 12 — migration rehearsal, controlled activation, and rollback

Do not treat passing unit tests as activation authorization.

### Test-mode rehearsal

- use a sandbox/test Asana project;
- run one agent at a time through Planning, Research, Small, Large, Evidence, Human Review, signoff, and movement;
- deliberately test stale candidate, out-of-band edit, uncertain write, migration failure, and move retry;
- verify Cooking reads only exact live `ready` tasks through the supported interface;
- verify no command exposes another stage’s protocol.

### Corpus migration

The corpus-wide migration follows `dish-docs-design.md`'s already-approved procedure, not a
separate live per-task flow: snapshot the complete target corpus to a tarball; give a fresh agent
the snapshot and final protocol bundle to produce the migrated corpus locally, removing legacy
structure without inventing content requiring judgment; run deterministic validation over every
result and return every structural failure for correction until the corpus passes; upload through a
script that stops rather than overwrites when a live task no longer matches its snapshot input;
never infer `ready`, provenance, Human decisions, or destination data. Retain the old
project/database snapshot as backup until accepted.

`dish-admin migrate` (Step 5's explicit migration command) is scoped narrowly to the ongoing case
this bulk procedure doesn't cover: a single older-schema task encountered individually after
cutover, not the initial corpus.

### Live-mode cutover

Before multi-agent use:

1. confirm shared service compatibility with current `DISH_VERSION` and schema, and confirm the approved GPT Action exposure/authentication route;
2. run the complete unit/integration suite;
3. run service concurrency and restart tests;
4. confirm GPT Action and CLI use the same endpoint and result contract;
5. remove/disable direct agent Asana write credentials for governed tasks and unsupported write paths, keeping Planning's read access to cooking history;
6. migrate the reviewed initial cohort;
7. verify one complete live task lifecycle;
8. open broader use only after the lock, drift, recovery, and audit checks pass.

### Rollback

Rollback must restore:

- the prior supported `honest` version/schema set;
- the compatible tool/service code;
- the shared database backup;
- task snapshots where a migration was applied.

Do not reopen writes under a mixed protocol/schema/tool combination.

### Completion gate

The tool is live only when protocol/schema compatibility, shared coordination, exact-content handling, migrations, and recovery have all been exercised successfully.

---

## Test strategy and required suite shape

Keep tests layered:

1. **Pure schema/parser tests** — no database or backend.
2. **Database/state-machine tests** — in-memory or temporary SQLite.
3. **Backend transaction tests** — fake Asana responses, timeouts, and ambiguous outcomes.
4. **Command tests** — full JSON envelope and audit assertions.
5. **Protocol integration fixtures** — exact current `honest` assets.
6. **Migration tests** — old task fixtures to current schema, including blocked ambiguous cases.
7. **Service tests** — concurrency, lease, auth, restart, and retry safety.
8. **Sandbox Asana tests** — separately authorized, never mixed into ordinary unit runs.

For every command test, assert:

- backend calls made or not made;
- exact live content identity before and after;
- task state block;
- local operation state;
- audit event;
- result code, retryability, allowed actions, and exit status;
- whether the operation is safe to repeat.

The existing 398-test pass remains the pre-change baseline. Replace assertions that encode obsolete family routing, `Verification:`, local-only prepare, destination gating, second-rejection escalation, task-pinned protocol bundles, or final-write-at-submit behaviour.

## Commit boundaries

Prefer one commit per implementation step. Where `honest` and `ai-tools` must change together for a schema/protocol contract, make the cross-repository relationship explicit in both commit messages and record the paired revisions in the implementation notes. Do not partially activate a schema change.

Suggested commit sequence:

1. `honest` version/schema baseline and resolver;
2. parser/renderer/migrations;
3. persistence/content identity;
4. backend transactions;
5. read/inspect/start/migrate;
6. prepare/live handoff;
7. Verification approve;
8. correction/stop routes;
9. movement/recovery;
10. docs/reports/agent hooks;
11. shared service and clients;
12. migration rehearsal and activation assets.

## Out of scope for this update

Unless separately approved, do not add:

- culinary semantic scoring by the tool;
- automatic Evidence or Human Review classification from free text;
- automatic migration during an ordinary command;
- permanent support for multiple old protocol/schema versions;
- direct agent access to Asana;
- broad generic workflow-engine abstractions unrelated to dish tasks;
- database-backend replacement beyond the shared V1 service store;
- speculative normalization beyond proven Asana transport behaviour.

## Definition of done

This update is complete when:

- all acceptance criteria in `dish-tool-update.md` pass;
- the current protocol/schema pair in `honest` is the only accepted pair;
- older tasks fail with `migration required` and can be explicitly migrated safely;
- every agent operation is tool-mediated;
- `prepare` writes and confirms the live candidate before handoff;
- Verification signs only the exact live candidate through an independent run;
- every correction/stop route behaves according to protocol;
- `submit` performs movement only;
- local single-agent mode is documented and tested;
- shared-service mode provides the lock and is mandatory for multi-agent live use;
- documentation, reports, fixtures, and tests describe the implemented behaviour rather than the obsolete lifecycle.
