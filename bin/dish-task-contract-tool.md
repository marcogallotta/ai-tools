# Dish Task Contract Tool — Design Draft

**Purpose:** Provide one controlled path for validating and writing complete contract-governed dish-task notes to Asana.

**Status:** Initial design. No implementation or production changes are authorized by this document.

## Scope

This tool governs complete writes to the notes of contract-managed dish tasks.

It is separate from the general-purpose Asana CLI, but the existing CLI must consult the contract tool’s registry before performing a generic note mutation.

This design does not attempt adversarial security. Agents are trusted to identify themselves and describe their work honestly. Mechanical controls exist to prevent accidental bypasses, stale writes, repeated writes, and incomplete validation.

A direct edit made through the Asana web UI or another integration bypasses this tool entirely and is not prevented — it is only caught reactively, either at the next `contract begin`'s baseline check against `modified_at`, or, for an edit made during an already-open cycle, at that cycle's `submit`-time freshness check.

## Current design decisions, pending formal approval

* Contract-managed notes cannot be changed through generic note-writing commands.
* This restriction can be relaxed later if it becomes obstructive.
* Agent identity is supplied explicitly as a trusted CLI flag.
* Agent identity is not cryptographically authenticated.
* Trusted state is stored in a local SQLite database at `~/ai-tools/var/dish-contract.db`, gitignored, shared by every locally-invoked agent regardless of family.
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

For `medium`/`large` changes, the verifier must explicitly confirm the declared level; if the verifier does not agree, no validation record is issued. `small` changes have no verifier at all (see Agent identity and verifier routing) — the declared level there is confirmed only by the mechanical checks in Deterministic validation, not by a second agent.

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

This mirrors the contract's own family definition ("GPT includes ChatGPT/Codex"), so a ChatGPT-authored cycle is routed the same as any other GPT-family edit — the opposite-family rule below already sends it to Claude, with no ChatGPT-specific case needed. ChatGPT cannot run the CLI itself; whoever runs `contract begin`/`submit` on its behalf declares `--agent gpt`.

Initial construction and large changes require verification by the opposite family.

Medium changes also require the opposite family, but verification may focus on the declared affected areas only when containment is accepted.

Small changes match the contract's Local change class: no verifier is required at all, and the task's existing `Verification` field is left as-is rather than reset. Step 4 (Semantic verification) is skipped entirely for a small change.

The final task process record must agree with:

* the declared final editor;
* the derived editor family;
* the declared change level;
* the required verifier family;
* the governing contract revision.

## Contract-managed task registry

Management is determined by the task's current section in the Cooking project (`1215089183018968`), checked live rather than fixed once at enrollment. Sections are identified by their immutable Asana section GID, not by display name — a section rename in Asana must not silently change which tasks are managed. The tool resolves the `Sourcing` and `Reference` sections' GIDs once by name at setup time and compares against those GIDs thereafter. A task is contract-managed unless its current section GID matches one of those two recorded GIDs; every other section — all cuisine sections, `Planned`, `Eating`, `Seasonal`, etc. — defaults to managed. If a task's section membership cannot be resolved to a GID at all (no section, or an API read failure), the tool fails closed and treats the task as managed rather than silently exempting it. This applies uniformly to new and pre-existing tasks: nothing needs a separate backfill or explicit enrollment pass, and moving a task into or out of the `Sourcing`/`Reference` GIDs changes its managed status from that point on.

Every generic note-mutation command performs a live Cooking-project section check before writing — the cache is not a substitute for this, since a task can move into or out of `Sourcing`/`Reference` through an allowed non-note command between checks. SQLite still records the determination per task (`managed_tasks`), but only for audit; it is never the sole source of truth for a live guard decision.

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

Before requesting validation, the agent performs its own end-to-end review of the complete note,
scoped the same way the contract already scopes verification by change level — local check for
`small`, the change and its identified dependencies for `medium`, the complete task for `large` (see
`dish-task-contract.md`'s Local/Delta/Reconstruction scoping). It records that review by writing a
`Self-verified: <agent>, <date>` line into the note's process record. This is not a separate tool
command: the line lives in the artifact itself and is checked mechanically by deterministic
validation (step 3) like any other required process-record line, so no separate self-verify command
or SQLite state is needed to keep it in sync with the content.

### 3. Deterministic validation

V1 validates the final file against a narrow, explicit rule set — mechanical checks only, no judgment about content quality or whether a section was rightly omitted:

* exactly one `CAN I COOK IT?` readiness line;
* `WHAT TO BUY` section present;
* process-record required lines present and syntactically well-formed (`Stage:`, `Human review:`, `Verification:`, `Self-verified:`);
* `Self-verified:`'s declared agent matches the cycle's current `editor_agent` — the only enforcement available for the self-review step; it cannot confirm the review's thoroughness, only that an attributable attestation exists in the exact content being validated;
* declared `--change-level` is one of `small`/`medium`/`large`, and matches the process record;
* editor/verifier family routing is internally consistent with the declared change level;
* contract revision recorded in the process record matches the revision captured at cycle-begin;
* no headings outside the canonical allowlist for the currently governing contract revision;
* no readiness contradiction between `CAN I COOK IT?` and the process record — `CAN I COOK IT? Yes`
  cannot coexist with `Human review: Pending - ...`, `Verification: Not done...`, or an open Delta or
  Reconstruction. Required by the change plan's approved deterministic-validation scope
  (`dish-task-contract-change-plan.md`, item 1); not deferred.

The last rule is deliberately revision-relative rather than a hardcoded legacy-field list: it checks the proposed final note against whatever the current contract defines as canonical, not against a static set of retired field names. Whatever a contract revision no longer defines — this round's legacy fields or a future one's — is excluded automatically, with no separate legacy-tracking logic needed. Reading an existing task is unconstrained (an old task may sit in an old format indefinitely); only a new write is held to the current contract's structure. The canonical allowlist should eventually be parsed from a machine-readable manifest carried in the contract file itself, once that contract-doc addition is approved (see `dish-task-contract-change-plan.md`), rather than duplicated by hand in this tool; until then, v1 uses a hardcoded allowlist mirroring the current contract and accepts the maintenance cost of updating it by hand when the contract's canonical headings change.

A cycle freezes the *contract* revision at `begin` (see Workflow, step 1). Before the machine-readable
canonical-structure manifest lands in the contract (a pending contract-doc addition — see
`dish-task-contract-change-plan.md`), v1's hardcoded allowlist is a property of the validator code,
not of the cycle, so the cycle record stores `validator_rules_version` — the hardcoded allowlist's own
version string — captured at `begin` and re-checked at every subsequent deterministic-validation pass.
A mismatch against the validator's current version invalidates the cycle, exactly as an Asana-side
task change invalidates a cycle's `modified_at` baseline, rather than silently applying new rules to a
cycle frozen against the old ones.

Once the manifest exists inside the contract text, this mechanism becomes unnecessary: the manifest is
covered by the cycle's existing `contract_text_hash` (Workflow, step 1), so a manifest change is
already detected as a contract-text change with no separate field needed. `validator_rules_version` is
a v1-only, pre-manifest field.

Deferred to a later version, once the mechanical layer is proven:

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
  --confirm-independent-review \
  --file <final-note>
```

`--confirm-independent-review` is a required, explicit flag the verifying agent must pass. It does not prevent one session from declaring itself both editor and verifier — identity is trusted, not authenticated (see Scope) — but it makes accidental self-verification visible rather than silent, and is logged to `audit_events`.

The verifier confirms:

* complete end-to-end semantic review;
* culinary and internal consistency;
* evidence adequacy;
* readiness;
* editor/verifier routing;
* the declared change level;
* containment for a medium change;
* the exact content being approved.

The verify command compares the submitted file's hash against the cycle's `pending_verification_hash`
— the hash of the content that most recently passed deterministic validation (step 3). A match means
unedited content; verification proceeds as above.

A mismatch means the verifier edited the note before signing. This is detected mechanically
(byte-level hash comparison), and the command then requires two additional flags before proceeding at
all:

```text
--change-level small|medium|large \
--change-reason "<brief explanation>"
```

Their absence on a hash mismatch is a hard reject: no validation record, no Asana mutation. The
declared level then branches:

* **`small`** — a Local correction. The cycle's `editor_agent`/`editor_family`/`change_level` are
  untouched, and the existing `Self-verified:` line is not rewritten: `small` maps to the contract's
  Local change class, which by definition "cannot change a material cooking, sourcing, safety,
  approval, or readiness outcome," and where "the prior signer did not verify this edit"
  (`dish-task-contract.md` line 149-150) — so the original self-verification remains valid for the
  edited content without re-attestation. The tool still re-runs step 3's deterministic validation
  against the new file — the `Self-verified:` agent-match check still passes since `editor_agent` is
  unchanged — before continuing to verify against it.
* **`medium`/`large`** — the verifier is now the new material editor, exactly as the contract already
  states ("supplying missing material evidence or replacing the recipe makes it the latest material
  editor and resets `Verification` to the opposite family" — `dish-task-contract.md` lines 199-200).
  The tool reassigns `editor_agent`/`editor_family`/`change_level`/`change_reason` on the *same*
  cycle — preserving one continuous audit trail rather than opening a new cycle — logs the escalation
  to `audit_events`, and re-runs step 3 against the new content. Step 3 now also requires the note's
  `Self-verified:` line to name this newly-assigned editor: the former verifier must self-verify their
  own edit, at the same contract-defined scope as any other editor, before anyone reviews it. On a
  step-3 pass, `pending_verification_hash` is updated to the new content's hash, and verification is
  now required from the family opposite the newly-assigned editor — which, since the original
  verifier was already opposite-family from the original editor, mechanically flips back to the
  original editor's family with no new routing logic.

Python never decides which of `small`/`medium`/`large` applies — that stays the verifier's own honest
declaration, the same trust basis as the editor's original `--change-level` ("Python does not infer
the semantic change level," see Current design decisions). The mechanism only enforces that a
declaration is made whenever content demonstrably changed, and keeps the bookkeeping (hashes,
routing, audit trail) consistent with whatever was declared.

If a validation record had already been issued for this cycle before an escalating edit is
discovered — a correction spotted late, between step 5 and `submit` — it is invalidated the same way
`submit`'s existing content-hash check already invalidates stale validation records (Workflow §6,
steps 2-4); no separate mechanism is needed for that narrower case.

### 5. Validation record and token

For `medium`/`large` changes, once deterministic validation (step 3) and semantic verification (step
4) both pass, the tool creates:

* one trusted validation record;
* one single-use write token.

For `small` changes, step 4 does not run (see Agent identity and verifier routing); the tool creates
the same two records once deterministic validation alone passes.

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
* verifier agent and family — null for a `small` record, since no verifier ran;
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

Recovery is a command in the contract admin tool (see below), not the agent-facing `contract` CLI, for both cases — an agent should not be able to interpret or resolve an ambiguous write outcome itself. It performs one targeted read of live notes and live `modified_at`, then applies this outcome table — notes state is the primary signal, `modified_at` state can only make the outcome stricter, never looser:

| Live notes match          | Live `modified_at` vs. baseline | Outcome                                                                                 |
| -------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------- |
| Intended final-content hash | unchanged or changed             | `consumed` — the write applied; notes are the write's own effect regardless of any incidental `modified_at` movement it caused. |
| Original baseline-notes hash | unchanged                        | `issued` — consistent with a confirmed failed write; the same validated content may be retried. |
| Original baseline-notes hash | changed                          | `revoked`, Marco-led recovery required — notes never changed, but something else touched the task after the baseline was captured, so per the standard staleness rule (any `modified_at` change invalidates a cycle) this cannot be treated as a clean, retriable `issued` state. |
| Neither                    | unchanged or changed             | `revoked`, Marco-led recovery required.                                                  |

## Contract admin tool

Marco-only actions live in a separate `contract-admin` command surface — a distinct
binary/subcommand namespace, not just a documented convention, so the boundary is unambiguous at the
command line rather than a naming similarity to the agent-facing `contract begin`/`verify`/`submit`/
`self-verify` commands that an agent could stumble into. Agents doing contract work are only ever
given the agent-facing `contract` surface. This is an operational and social convention, not a
technical secret: this design document and the tool's own code are both agent-readable, so
`contract-admin`'s existence and commands cannot be treated as genuinely undiscoverable. The actual
boundary is that agents are not instructed or expected to look for or invoke it, consistent with the
"not adversarial security" framing in Scope — it is not a permission check the tool enforces at
runtime, and no claim of technical secrecy is made.

`contract-admin` covers:

* `contract-admin recover <cycle-id>` — resolve a stuck `in_flight` or `uncertain` token after a process crash or an ambiguous Asana response;
* replacing a token after it is consumed, revoked, or stuck in an ambiguous recovery state that resolves as such — this is always a brand-new cycle, validation record, and token for a fresh review pass, never the reactivation or reuse of the old, already-consumed token record; a consumed or revoked token itself remains permanently unusable;
* other Marco-only actions identified later.

Revoking a task's contract-managed status is not a feature of `contract-admin`, or of this design at
all. The general-purpose Asana CLI is guarded the same as any other caller (see Contract-managed task
registry) and gives Marco no bypass — nothing about being Marco is authenticated or distinguishable
to it. If a managed task genuinely needs a one-off manual edit outside the guarded workflow, Marco
makes it directly through the Asana web UI instead — the same documented bypass already named in
Scope (line 15): a direct edit there isn't prevented, only caught reactively at the next baseline
check.

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
* `validator_rules_version`
* `editor_agent`
* `editor_family`
* `change_level`
* `change_reason`
* `pending_verification_hash`
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

ChatGPT has no local CLI or SQLite access, so it cannot run any `contract` command itself.

Its output is one complete final-note file.

A local agent or Marco then:

1. begins or resumes the contract cycle, declaring `--agent gpt` — this attributes the cycle to ChatGPT as editor even though a local process runs the command on its behalf;
2. runs deterministic validation;
3. performs semantic verification, required from the opposite (Claude) family per the standard routing rule — nothing ChatGPT-specific, since GPT and Codex are one family (see Agent identity and verifier routing);
4. creates the trusted SQLite validation record and token;
5. submits the exact file.

ChatGPT cannot self-issue a trusted validation record, declare its own `--agent` value, or perform `--confirm-independent-review` itself — a human or local agent does so on its behalf, honestly reflecting who actually reviewed the note.

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
* stuck `in_flight` token recovered via manual `contract recover` command;
* `Self-verified:` line missing or naming an agent other than `editor_agent`;
* verifier submission matching `pending_verification_hash` (no edit) proceeds without requiring
  `--change-level`/`--change-reason`;
* verifier submission with a hash mismatch and no `--change-level`/`--change-reason` is rejected;
* verifier-declared `small` edit re-validates in place with `editor_agent` unchanged;
* verifier-declared `medium`/`large` edit reassigns `editor_agent`/`editor_family`/`change_level` on
  the same cycle, updates `pending_verification_hash`, and flips required verifier family back to the
  original editor's family;
* `small`-only cycle creates a validation record/token after deterministic validation alone, with
  `verifier_agent`/`verifier_family` null;
* `CAN I COOK IT? Yes` rejected alongside `Human review: Pending`, `Verification: Not done`, or an
  open Delta/Reconstruction;
* `validator_rules_version` mismatch at re-validation invalidates the cycle;
* section-GID resolution: a `Sourcing`/`Reference` rename does not change managed status; an
  unresolvable section fails closed to managed.

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
* provide a dedicated revoke-management command (Marco uses the Asana web UI directly instead, per Contract admin tool).

## Open decisions

1. **Initial management — resolved:** No explicit enrollment step. A task is contract-managed by default based on its current Cooking-project section, excluding only `Sourcing` and `Reference` (see Contract-managed task registry).
2. **Marco-only actions — resolved:** All Marco-only actions (`contract recover`, token issuance/replacement) live in a single contract admin tool, separate from and invisible to the agent-facing `contract` commands (see Contract admin tool). Revoking a task's contract-managed status is not a feature of this design: Marco uses the existing general-purpose Asana CLI directly, which agents never have access to or knowledge of.
3. **Token lifetime — resolved:** No automatic expiry. A stuck `in_flight` token requires an explicit, manually run `contract recover` command (see Failure behaviour); no background timeout or heartbeat-based auto-recovery in v1.
4. **Verifier edits — resolved:** No threshold is inferred by Python. A hash mismatch between the
   verifier's submitted file and `pending_verification_hash` requires an explicit
   `--change-level`/`--change-reason` declaration; `medium`/`large` reassigns
   `editor_agent`/`editor_family`/`change_level` on the same cycle and re-routes to the opposite
   family; `small` re-validates in place with no reassignment (see Workflow, step 4).
5. **Existing tasks — resolved:** Every pre-existing task in the Cooking project outside `Sourcing`/`Reference` is contract-managed immediately, per the same live section-based rule as new tasks. No separate enrollment pass is needed; whether existing tasks' *content* needs migration to the current canonical structure is a separate question, unaffected by this (see Out of scope).

