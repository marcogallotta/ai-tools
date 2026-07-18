# Dish Task Contract Tool — Design Draft

**Purpose:** Provide one controlled path for validating and writing complete contract-governed dish-task notes to Asana.

**Status:** Initial design. No implementation or production changes are authorized by this document.

## Scope

This tool governs complete writes to the notes of contract-managed dish tasks.

It is separate from the general-purpose Asana CLI, but the existing CLI must consult the contract tool’s registry before performing a generic note mutation.

This design does not attempt adversarial security. Agents are trusted to identify themselves and describe their work honestly. Mechanical controls exist to prevent accidental bypasses, stale writes, repeated writes, and incomplete validation.

A direct edit made through the Asana web UI or another integration bypasses this tool entirely and is not prevented — it is only caught reactively, at the next `contract begin`'s baseline check against `modified_at`.

## Current design decisions, pending formal approval

* Contract-managed notes cannot be changed through generic note-writing commands.
* This restriction can be relaxed later if it becomes obstructive.
* Agent identity is supplied explicitly as a trusted CLI flag.
* Agent identity is not cryptographically authenticated.
* Trusted state is stored in a local SQLite database.
* Any change to the Asana task after the initial read invalidates the baseline.
* Baseline freshness is checked through Asana’s `modified_at`.
* The editing agent declares a **change level** and explains why.
* User-facing change levels are **small**, **medium**, and **large**.
* Python does not infer the semantic change level.
* The complete final note is validated and written as one artifact.
* A successful write consumes a single-use token.
* An identical second write is rejected.

## Change levels

The tool uses plain-language terms deliberately distinct from the contract's own change-class
vocabulary, so an agent pattern-matching text cannot conflate the tool's field with the contract's
field. The declared level maps directly onto the contract's change class for the process record:

```text
small  → Local
medium → Delta
large  → Reconstruction
```

The tool uses the plain-language terms; the task process record uses the contract terms.

### Small change

A change that cannot materially alter cooking, sourcing, safety, halal compliance, readiness, Human approval, or the intended result.

Examples may include spelling, formatting, or an unambiguous correction with no material downstream effect.

### Medium change

A material change whose consequences are clearly limited to identified parts of the task.

The editor must identify the affected parts and explain why the effect is contained.

### Large change

A change that can affect the complete construction, or whose consequences cannot be confidently contained.

Initial task construction is always treated as a large change.

The editing agent declares:

```text
--change-level small|medium|large
--change-reason "<brief explanation>"
```

The verifier must explicitly confirm the declared level. If the verifier does not agree, no validation record is issued.

## Agent identity and verifier routing

Every editing or verification operation requires:

```text
--agent claude|gpt|codex
```

The value is trusted as an honest declaration.

Agent families are:

```text
claude          → Claude family
gpt, codex      → GPT family
```

Initial construction and large changes require verification by the opposite family.

Medium changes also require the opposite family, but verification may focus on the declared affected areas only when containment is accepted.

Small changes do not require a new independent verification pass unless a deterministic check or the agent’s own review identifies a material consequence.

The final task process record must agree with:

* the declared final editor;
* the derived editor family;
* the declared change level;
* the required verifier family;
* the governing contract revision.

## Contract-managed task registry

Management is determined by the task's current section in the Cooking project (`1215089183018968`), checked live rather than fixed once at enrollment. A task is contract-managed unless its current section is `Sourcing` or `Reference`; every other section — all cuisine sections, `Planned`, `Eating`, `Seasonal`, etc. — defaults to managed. This applies uniformly to new and pre-existing tasks: nothing needs a separate backfill or explicit enrollment pass, and moving a task into or out of `Sourcing`/`Reference` changes its managed status from that point on.

SQLite still caches the current determination per task (`managed_tasks`) for audit and to avoid a live Asana lookup on every check, but the cache reflects the task's live section membership rather than being the sole source of truth.

A task remains contract-managed after a successful write, so long as its section hasn't moved it out of management. Any later note change requires another contract cycle.

Generic commands must consult this determination before mutating notes.

The guard applies to:

* `set-notes`;
* `append`;
* `replace`;
* batch operations that update notes;
* `raw` writes containing `notes` or `html_notes`.

Unrelated operations such as renaming, moving, completing, or changing other fields remain outside this contract unless later expanded.

The contract submission path uses an internal guarded write operation after all checks pass. It does not disable its own write through the generic-command guard.

## Workflow

### 1. Begin cycle

The editor starts a cycle with:

```text
contract begin <task-gid> \
  --agent <agent> \
  --change-level <level> \
  --change-reason "<reason>"
```

The tool:

1. Confirms the task exists.
2. Reads the complete task.
3. Records its `modified_at`.
4. Records a hash of the current notes for diagnostics and recovery.
5. Reads the exact governing contract.
6. Derives the contract revision.
7. Confirms the task is contract-managed under the enrolment policy.
8. Creates an open cycle in SQLite.
9. Exports the current note as the working-file starting point.

The cycle binds to the contract revision captured at `begin`: a later edit to `dish-task-contract.md` does not affect an already-open cycle, matching the contract's own freeze-through-signoff rule. The cycle record stores a hash of the exact governing contract text used, not only the human-readable revision string, so a mismatch between the recorded revision and the text actually used is detectable.

The baseline applies to the whole task, not only its notes.

Any later change that alters `modified_at` invalidates the cycle. This deliberately uses a stricter
baseline than the change plan's note-content-only proposal: any task change, not only a note-content
change, invalidates the cycle. The notes hash recorded at cycle start is kept for recovery and
diagnosis, not as the primary staleness check.

Before implementation, this must be tested against real Asana behaviour to confirm which operations
(comments, custom-field changes, section moves, etc.) actually bump `modified_at`, since an
over-sensitive baseline would invalidate cycles on activity unrelated to the note content.

### 2. Construct final note

The agent edits a local file containing the complete proposed final task note.

Patches, replacement fragments, and incremental Asana edits are not accepted by the contract submission path.

The agent must review the complete assembled note before requesting validation.

### 3. Deterministic validation

V1 validates the final file against a narrow, explicit rule set — mechanical checks only, no judgment about content quality or whether a section was rightly omitted:

* exactly one `CAN I COOK IT?` readiness line;
* `WHAT TO BUY` section present;
* process-record required lines present and syntactically well-formed (`Stage:`, `Human review:`, `Verification:`);
* declared `--change-level` is one of `small`/`medium`/`large`, and matches the process record;
* editor/verifier family routing is internally consistent with the declared change level;
* contract revision recorded in the process record matches the revision captured at cycle-begin;
* no headings outside the canonical allowlist for the currently governing contract revision.

The last rule is deliberately revision-relative rather than a hardcoded legacy-field list: it checks the proposed final note against whatever the current contract defines as canonical, not against a static set of retired field names. Whatever a contract revision no longer defines — this round's legacy fields or a future one's — is excluded automatically, with no separate legacy-tracking logic needed. Reading an existing task is unconstrained (an old task may sit in an old format indefinitely); only a new write is held to the current contract's structure. The canonical allowlist should eventually be parsed from a machine-readable manifest carried in the contract file itself, once that contract-doc addition is approved (see `dish-task-contract-change-plan.md`), rather than duplicated by hand in this tool; until then, v1 uses a hardcoded allowlist mirroring the current contract and accepts the maintenance cost of updating it by hand when the contract's canonical headings change.

Deferred to a later version, once the mechanical layer is proven:

* deterministic readiness contradictions (e.g. `CAN I COOK IT? Yes` with `Human review: Pending`);
* unresolved structural placeholder detection (e.g. leftover `[approx]` markers);
* any judgment of whether an omitted section should have been present, or of content quality — these remain the verifier's job, not the validator's, for the foreseeable future.

The validator reports every detected failure and performs no Asana mutation.

The deterministic validator does not decide whether the recipe is culinarily correct, whether research is adequate, or whether the declared change level is semantically honest.

### 4. Semantic verification

The required verifier reviews the complete proposed note.

The verification command requires:

```text
contract verify <cycle-id> \
  --agent <verifier-agent> \
  --file <final-note>
```

The verifier confirms:

* complete end-to-end semantic review;
* culinary and internal consistency;
* evidence adequacy;
* readiness;
* editor/verifier routing;
* the declared change level;
* containment for a medium change;
* the exact content being approved.

If the verifier materially edits the note, the edited content must be treated as a new final artifact and validated again before a record is issued. The exact threshold for "materially" remains undefined (see Open decisions); until resolved, a verifier should treat any edit beyond wording or formatting as material and err toward re-validation.

### 5. Validation record and token

After deterministic and semantic validation pass, the tool creates:

* one trusted validation record;
* one single-use write token.

Neither needs to be a portable signed file. Their trust comes from being stored and state-managed by the local tool in SQLite.

The validation record binds:

* cycle ID;
* task GID;
* final content hash;
* baseline `modified_at`;
* baseline notes hash;
* contract revision;
* editor agent and family;
* change level and reason;
* verifier agent and family;
* validator version;
* validation time.

The token binds:

* cycle ID;
* task GID;
* validation-record ID;
* final content hash;
* baseline `modified_at`;
* current token state.

The token is an internal database record, not a security credential that the agent must keep secret.

### 6. Guarded submission

Submission uses:

```text
contract submit <cycle-id> --file <final-note>
```

The tool:

1. Loads the cycle, validation record, and token.
2. Re-runs deterministic validation.
3. Recomputes the exact final-content hash.
4. Rejects if it differs from the validated hash.
5. Reads the Asana task immediately before mutation.
6. Compares current `modified_at` with the initial baseline.
7. Rejects if they differ.
8. Atomically changes the token from `issued` to `in_flight` in SQLite.
9. Sends one complete notes update to Asana.
10. On clear success, marks the token `consumed` and the cycle `completed`.

The pre-write read adds one Asana round trip. This is intentional.

The design accepts that a small race remains between the freshness check and the Asana mutation unless Asana provides a usable conditional-update mechanism.

## Token states

```text
issued
in_flight
consumed
uncertain
revoked
```

Allowed transitions:

```text
issued → in_flight
in_flight → consumed
in_flight → issued
in_flight → uncertain
issued → revoked
uncertain → consumed
uncertain → issued
uncertain → revoked
```

A consumed or revoked token cannot be reused.

A second submission using a consumed token fails even when the content is identical.

## Failure behaviour

### Failure before mutation

Examples:

* deterministic validation failure;
* missing verification;
* content-hash mismatch;
* routing mismatch;
* invalid token;
* stale `modified_at`.

No Asana write occurs.

A stale baseline revokes the current token and closes the cycle as stale. A new cycle must begin from the new task state.

A wrong-file or hash-mismatch rejection does not consume the token.

### Confirmed API failure

When Asana clearly rejects the request and the tool knows the write was not applied, the token returns from `in_flight` to `issued`.

The same validated submission may be retried after the cause is addressed.

### Uncertain API outcome

A timeout, lost response, connection break, or similar ambiguous result changes the token to `uncertain`.

The tool must not blindly retry, and the agent-facing surface has no way to resolve this itself — it is the same class of judgment call as a crashed process below, and is resolved the same way.

### Crashed process or uncertain outcome (stuck `in_flight` / `uncertain`)

If the tool's own process dies while a token is `in_flight`, or a submission returns an ambiguous result and the token is `uncertain`, nothing recovers it automatically — no timeout, no background sweep, no automatic retry. The stuck task simply stays unavailable for a new cycle until recovered; nothing about this blocks an agent from continuing other work, including other tasks' cycles.

Recovery is a command in the contract admin tool (see below), not the agent-facing `contract` CLI, for both cases — an agent should not be able to interpret or resolve an ambiguous write outcome itself. It performs one targeted read:

* If live notes match the intended final-content hash, mark the token `consumed`.
* If live notes match the original baseline-notes hash and the task state is otherwise consistent with a failed write, return the token to `issued`.
* If live notes match neither, revoke the token and require further Marco-led recovery.

## Contract admin tool

Marco-only actions live in a separate contract admin tool, distinct from the agent-facing `contract begin`/`verify`/`submit` commands. Agents doing contract work are only ever given the agent-facing surface — the admin tool's existence, commands, and location are not documented to them and not discoverable from this design or the tool's code. Only Marco runs it. This is a separation-of-knowledge control, not a permission check the tool enforces at runtime, consistent with the "not adversarial security" framing in Scope.

The admin tool covers:

* `contract recover <cycle-id>` — resolve a stuck `in_flight` or `uncertain` token after a process crash or an ambiguous Asana response;
* issuing or replacing a write token — e.g. after a consumed token needs a genuine re-write, or an ambiguous recovery state needs Marco's explicit resolution;
* other Marco-only actions identified later.

Revoking a task's contract-managed status is not a feature of the admin tool, or of this design at all. Marco always retains direct access to the existing general-purpose Asana CLI, which agents doing contract work are never given or told about; if a managed task genuinely needs a one-off manual edit outside the guarded workflow, Marco makes it directly through that existing tool instead.

## SQLite model

Minimum tables:

### `managed_tasks`

* `task_gid`
* `managed_since`
* `status`
* `current_cycle_id`

### `cycles`

* `cycle_id`
* `task_gid`
* `baseline_modified_at`
* `baseline_notes_hash`
* `contract_revision`
* `contract_text_hash`
* `editor_agent`
* `editor_family`
* `change_level`
* `change_reason`
* `status`
* `created_at`
* `completed_at`

### `validation_records`

* `validation_record_id`
* `cycle_id`
* `content_hash`
* `verifier_agent`
* `verifier_family`
* `validator_version`
* `validated_at`

### `write_tokens`

* `token_id`
* `cycle_id`
* `validation_record_id`
* `content_hash`
* `state`
* `issued_at`
* `updated_at`

### `audit_events`

* `event_id`
* `cycle_id`
* `event_type`
* `actor_agent`
* `details`
* `created_at`

SQLite transactions protect local state changes and prevent two local submissions from consuming the same token.

## Content hashing

The validation record must bind to the exact content sent to Asana.

Initial canonicalization proposal:

* UTF-8 encoding;
* LF line endings;
* no trimming;
* no automatic whitespace cleanup;
* no section reordering;
* no silent markdown rewriting;
* hash algorithm: SHA-256;
* canonicalization version stored with the record.

The tool hashes the canonical bytes and sends the corresponding decoded text.

Before implementation, this must be tested against an Asana write/read round trip to determine whether Asana normalizes trailing newlines or other note content. The canonicalization rule must reflect observable API behaviour so uncertain-outcome recovery is reliable.

## Integration with the existing Asana CLI

The contract tool and general Asana CLI may share:

* SDK client construction;
* task reads;
* task updates;
* error formatting.

They must not share unguarded note-writing behaviour.

The general CLI asks the contract registry whether a target task is managed before changing notes.

The contract tool performs its final update through a separate guarded gateway that cannot be called without a valid cycle, validation record, and token.

## ChatGPT workflow

ChatGPT cannot perform the trusted local submission.

Its output is one complete final-note file.

A local agent or Marco then:

1. begins or resumes the contract cycle;
2. runs deterministic validation;
3. performs semantic verification;
4. creates the trusted SQLite validation record and token;
5. submits the exact file.

ChatGPT cannot self-issue a trusted validation record.

## Direct dependencies

Dependency surfacing is advisory and does not block token issuance in the first implementation.

A later scanner may surface only bounded direct candidates:

* exact task-GID references;
* explicit Asana links;
* exact task-name references;
* clearly named planning documents.

It must not recursively audit dependencies or decide semantic impact.

## Testing requirements

Implementation follows TDD.

Tests must cover:

* SQLite schema and migrations;
* task registration;
* generic note-write blocking;
* non-note generic writes remaining allowed;
* declared agent-name validation;
* agent-family routing;
* small, medium, and large change-level handling;
* initial construction treated as large;
* verifier-family mismatch;
* deterministic contract failures;
* exact content-hash binding;
* content changed after validation;
* any `modified_at` change causing stale rejection;
* no Asana mutation on any pre-write failure;
* exactly one Asana mutation on success;
* token reuse rejection;
* two simultaneous submissions using one token;
* confirmed API failure preserving retry eligibility;
* uncertain outcome where the write succeeded;
* uncertain outcome where the write did not apply;
* uncertain outcome with a third, conflicting state;
* raw `notes` and `html_notes` bypass attempts;
* task remaining managed after successful submission;
* stuck `in_flight` token recovered via manual `contract recover` command.

## Out of scope

The first implementation does not:

* cryptographically authenticate agents;
* infer change level from note text;
* decide semantic culinary correctness;
* recursively audit dependencies;
* govern non-note task fields;
* automatically migrate every existing dish task;
* modify the contract text or incident logs;
* provide a remote or multi-user trust service;
* document the contract admin tool's location or invocation for agents;
* provide a dedicated revoke-management command (Marco uses the existing general-purpose Asana CLI directly instead).

## Open decisions

1. **Initial management — resolved:** No explicit enrollment step. A task is contract-managed by default based on its current Cooking-project section, excluding only `Sourcing` and `Reference` (see Contract-managed task registry).
2. **Marco-only actions — resolved:** All Marco-only actions (`contract recover`, token issuance/replacement) live in a single contract admin tool, separate from and invisible to the agent-facing `contract` commands (see Contract admin tool). Revoking a task's contract-managed status is not a feature of this design: Marco uses the existing general-purpose Asana CLI directly, which agents never have access to or knowledge of.
3. **Token lifetime — resolved:** No automatic expiry. A stuck `in_flight` token requires an explicit, manually run `contract recover` command (see Failure behaviour); no background timeout or heartbeat-based auto-recovery in v1.
4. **Verifier edits:** When a verifier changes content, what exact threshold makes the verifier the new material editor and therefore requires verification by the opposite family?
5. **Existing tasks — resolved:** Every pre-existing task in the Cooking project outside `Sourcing`/`Reference` is contract-managed immediately, per the same live section-based rule as new tasks. No separate enrollment pass is needed; whether existing tasks' *content* needs migration to the current canonical structure is a separate question, unaffected by this (see Out of scope).
6. **SQLite trust-store location and cross-family access:** Where does the SQLite trust store live, and how do both Claude-family and GPT-family agents — potentially separate processes or environments — reach the same cycle and validation-record state to complete a cross-family verification handoff?
7. **Small-change verification path:** When a small change skips independent verification, does workflow step 4 (Semantic verification) get skipped entirely, and what does the deterministic check's "editor/verifier family routing is internally consistent with declared change level" mean when there is no verifier?
8. **ChatGPT-authored cycle attribution:** What `--agent` value represents ChatGPT as editor when a local agent or Marco runs the cycle on its behalf, and how is opposite-family verification guaranteed rather than accidentally same-family?
9. **Non-authenticated identity — explicit sign-off needed:** The design narrows the change plan's "authenticated editor identity" language to trusted-but-non-authenticated, declared via CLI flag. Confirm this is the accepted scope rather than an unapproved narrowing.
10. **Self-verification risk — explicit acceptance needed:** Nothing prevents one session from declaring itself both editor and opposite-family verifier (e.g. `--agent claude` then later `--agent codex`) with no actual second review occurring. Confirm this is an accepted consequence of the "not adversarial security" framing, since it materially weakens the verifier-routing protection the change plan treats as approved.

