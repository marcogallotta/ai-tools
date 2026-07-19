# Dish Task Contract Tool — Design Draft

**Purpose:** Provide one controlled path for validating and writing complete contract-governed dish-task notes to Asana.

**Status:** Initial design, v1 scope only. No implementation or production changes are authorized by
this document. Everything not needed for v1 to exist and work — v1b's enforcement flip, v2 candidate
features, and ideas considered and rejected outright — lives in
`dish-task-contract-tool-future.md`, not here.

## Scope

This tool governs complete writes to the notes of contract-managed dish tasks.

It is separate from the general-purpose Asana CLI. The existing CLI must consult this tool's live
managed-task determination before performing a generic note mutation — during v1a that consultation
is advisory/log-only (see `dish-task-contract-tool-future.md`, Versioning plan); v1b makes it a hard block.

This design does not attempt adversarial security. Agents are trusted to identify themselves and
describe their work honestly. Mechanical controls exist to prevent accidental bypasses, stale writes,
repeated writes, and incomplete validation.

Direct web or integration edits are not prevented and are not generally identifiable as bypasses.
`contract start` claims an exclusive lock on the task and captures the baseline once, at that moment,
before drafting begins; while the lock is held, no other `contract` CLI caller can start work on the
same task, and `submit` performs no further staleness re-check (see Workflow). This does not cover an
edit made directly outside the guarded tool entirely — e.g. via Asana's UI, or the generic CLI's
advisory-only v1a mode — which is invisible to the lock and is only caught, if at all, by
`contract-admin recover`'s outcome table when resolving a crashed/uncertain submission.

## Current design decisions, pending formal approval

* Contract-managed notes cannot be changed through generic note-writing commands once v1b is enabled;
  in v1a the restriction is advisory/logged only.
* Agent identity is supplied explicitly as a trusted CLI flag, not cryptographically authenticated.
* Trusted state is stored in a local SQLite database at `~/ai-tools/var/dish-contract.db`, gitignored,
  shared by every locally-invoked agent regardless of family.
* `contract start` claims an exclusive per-task lock, held by one `submissions` row from `drafting`
  through any terminal state; the `submission_id` it creates is used as the token for every later
  command on that submission. The baseline (`baseline_modified_at`/`baseline_notes_hash`) is captured
  once, at `start`, not re-read for staleness later — see Workflow.
* The editing agent declares a **change level** (`small`/`medium`/`large`) and a reason; Python does
  not infer it.
* The complete final note is validated and written as one artifact — no patches or fragments.
* A successful write consumes a single-use submission; an identical second write is rejected.
* A submission may attempt at most 3 actual Asana writes (`write_count`); a 4th attempt is rejected
  outright and requires `contract-admin` to reset or replace the submission before the task can be
  written again.

## Change levels

The tool uses plain-language terms deliberately distinct from the contract's own change-class
vocabulary, so an agent pattern-matching text cannot conflate the tool's field with the contract's
field. The declared level maps directly onto the contract's change class for the process record:

```text
small  → Local
medium → Delta
large  → Reconstruction
```

### Small change

A change that cannot materially alter cooking, sourcing, safety, halal compliance, readiness, Human
approval, or the intended result. Examples may include spelling, formatting, or an unambiguous
correction with no material downstream effect.

### Medium change

A material change whose consequences are clearly limited to identified parts of the task. The editor
must identify the affected parts and explain why the effect is contained.

### Large change

A change that can affect the complete construction, or whose consequences cannot be confidently
contained. Initial task construction is always treated as a large change.

The editing agent declares:

```text
--change-level small|medium|large
--change-reason "<brief explanation>"
```

`medium`/`large` require an opposite-family verifier to `approve` before submission. `small` requires
no verifier at all — confirmed only by deterministic validation, not by a second agent (see Agent
identity and verifier routing).

## Agent identity and verifier routing

Every command requires:

```text
--agent claude|gpt|codex
```

trusted as an honest declaration. Agent families:

```text
claude          → Claude family
gpt, codex      → GPT family
```

This mirrors the contract's own family definition ("GPT includes ChatGPT/Codex"), so a ChatGPT-authored
submission routes the same as any other GPT-family edit. ChatGPT cannot run the CLI itself; whoever
runs `contract start`/`prepare`/`submit` on its behalf declares `--agent gpt`.

Initial construction and `large` changes require verification by the opposite family. `medium` also
requires the opposite family, but the verifier may focus review on the declared affected areas when
containment is accepted. `small` matches the contract's Local change class: no verifier runs, and the
task's existing `Verification` field is left as-is.

The opposite-family requirement on `approve` makes `editor_agent == verifier_agent` structurally
unreachable — it fails the family check before any further comparison would matter. The residual risk
of one session dishonestly declaring different agent values for editing and verification is not
detectable under the trusted-identity model (see Scope) and is not claimed to be caught here.

The final task process record must agree with the declared final editor, its derived family, the
declared change level, the required verifier family, and the governing contract revision.

## Contract-managed task registry

Management is determined by the task's current section in the Cooking project (`1215089183018968`),
checked live rather than fixed once at enrollment. Sections are identified by their immutable Asana
section GID, not by display name — a section rename must not silently change which tasks are managed.
The tool resolves the `Sourcing` and `Reference` sections' GIDs once by name at setup time and compares
against those GIDs thereafter. A task is contract-managed unless its current section GID matches one of
those two — every other section defaults to managed. If section membership cannot be resolved to a GID
at all, the tool fails closed and treats the task as managed. This applies uniformly to new and
pre-existing tasks; no separate enrollment or backfill pass is needed.

**v1a:** the generic CLI still performs a live check before a note mutation on a managed task, but only
to log an advisory bypass event (task GID, command used, agent if known) — the write proceeds. **v1b:**
the same check rejects the write instead.

The check applies to `set-notes`, `append`, `replace`, batch operations updating notes, and `raw`
writes containing `notes`/`html_notes`. Unrelated operations (rename, move, complete, other fields)
remain outside this contract unless later expanded.

The contract submission path uses an internal guarded write operation after all checks pass; it does
not go through the generic-command guard at all, in either v1a or v1b.

## Workflow

### 1. `contract start`

```text
contract start <task-gid> --agent claude|gpt|codex
```

Claims the exclusive lock on the task and opens the submission that every later command in this
workflow operates on.

1. Confirms the task exists.
2. Confirms no other open submission already exists for this task — enforced by application check and
   by a partial unique index on `submissions(task_gid)` for non-terminal `status` (including
   `drafting`), so a race between two simultaneous `start` calls fails at the database layer, not only
   in application logic. This is the lock: two agents cannot both `start` the same task.
3. Reads the complete task; records `baseline_modified_at` and `baseline_notes_hash`. This is the only
   baseline read for the submission's entire life — closing the gap where a long drafting window could
   silently miss an intervening edit.
4. Creates one `submissions` row, status `drafting`, `write_count` 0. The row's `submission_id` is
   printed back to the caller and used as the token for every subsequent command
   (`prepare`/`approve`/`reject`/`submit`) on this submission — there is no separate token object.

The lock is held for as long as the submission stays in a non-terminal status, and releases
automatically when the submission reaches a terminal state (`consumed`, `stale`, `rejected`) — see
Submission states.

### 2. `contract prepare`

```text
contract prepare <submission-id> \
  --agent claude|gpt|codex \
  --change-level small|medium|large \
  --change-reason "<reason>" \
  --file <final-note>
```

Before running this, the agent has already assembled one complete canonical note (no patches or
fragments accepted anywhere in this workflow) and performed its own end-to-end self-review, scoped by
change level the same way the contract already scopes verification — local check for `small`, the
change and its identified dependencies for `medium`, the complete task for `large`. It records that
review by writing `Self-verified: <agent>, <date>` into the file's process record; this is checked
mechanically, not through a separate command.

The tool:

1. Confirms the submission exists and is in status `drafting`. It does not take its own fresh baseline
   read — it uses `baseline_modified_at`/`baseline_notes_hash` already captured on the row by `start`.
2. Reads the exact governing contract text (from `$CONTRACT_MD_PATH`, defaulting to
   `~/honest-pantry/dish-task-contract.md`), including its embedded canonical-structure manifest, and
   stores the parsed manifest as `canonical_manifest` alongside `contract_revision` and
   `contract_text_hash` — this submission is validated against this exact frozen manifest for its
   entire life, matching the contract's own freeze-through-signoff rule; a later contract edit does not
   affect an already-open submission.
3. Runs deterministic validation (below) against `<final-note>`.
4. On a validation failure: reports every violated rule; the submission stays in `drafting` (it already
   exists, opened by `start`), but the attempt is logged (see Logging and observability) so failure
   patterns are visible even before a validation pass.
5. On a pass: hashes the file as `content_hash`, computes a compact diff summary against
   `baseline_notes_hash`'s source text (`characters_added`, `characters_removed`, `lines_changed`,
   `headings_touched`), logs that summary to `audit_events`, and advances the row out of `drafting`. The
   summary is computed and logged, not persisted as a second full copy of the note — hashes alone
   cannot answer what a `small`-declared change actually touched (see Logging and observability), so
   this is the minimum needed to make that observability claim true.
   * `small` → status `ready`, no verifier required.
   * `medium`/`large` → status `awaiting_verification`, with `required_verifier_family` set to the
     family opposite `editor_family`.

Deterministic validation checks, mechanically only — no judgment about content quality or whether a
section was rightly omitted:

* exactly one `CAN I COOK IT?` readiness line;
* `WHAT TO BUY` section present;
* process-record required lines present and well-formed (`Stage:`, `Human review:`, `Verification:`,
  `Self-verified:`);
* `Self-verified:`'s declared agent matches `editor_agent` — the only enforcement available for the
  self-review step; it cannot confirm thoroughness, only that an attributable attestation exists for
  the exact bytes submitted;
* declared `--change-level` matches the process record, and editor/verifier family routing is
  internally consistent with it;
* contract revision recorded in the process record matches this submission's `contract_revision`;
* no headings outside `canonical_manifest`'s allowlist;
* no readiness contradiction — `CAN I COOK IT? Yes` cannot coexist with `Human review: Pending - ...`,
  `Verification: Not done...`, or an open Delta/Reconstruction (required by the change plan's approved
  deterministic-validation scope, item 1; not deferred).

The canonical allowlist is parsed from the manifest carried in the contract text itself, not
duplicated by hand in this tool — required as part of v1a's scope, not a later addition. A
hand-maintained hardcoded allowlist would recreate, inside the validator meant to eliminate this exact
failure mode, the same silent-drift risk the tool exists to remove from the contract's own prose rules.

Deferred to v2 once the mechanical layer is proven: unresolved structural placeholder detection (e.g.
leftover `[approx]` markers); any judgment of whether an omitted section should have been present, or
of content quality — these remain the verifier's job, not the validator's.

The validator performs no Asana mutation and does not decide whether the recipe is culinarily correct,
whether research is adequate, or whether the declared change level is semantically honest.

### 3. `contract approve` / `contract reject`

Required only for `medium`/`large`. The verifier reviews the exact prepared file — culinary and
internal consistency, evidence adequacy, readiness, editor/verifier routing, the declared change level,
containment for a `medium` change, and that the file is exactly the content being approved.

```text
contract approve <submission-id> --agent <verifier-agent> --file <same-final-note>
```

* Requires `verifier-agent`'s family to be the submission's `required_verifier_family`.
* Requires the submitted file's hash to exactly match `content_hash` (see Content hashing for what
  "exact match" means). The verifier has no path to submit modified content through this command.
* On pass: records `verifier_agent`/`verifier_family`, sets status `ready`.

```text
contract reject <submission-id> --agent <verifier-agent> --reason "<why not signable>"
```

* Requires `verifier-agent`'s family to be the submission's `required_verifier_family`, exactly as
  `approve` does — rejection is part of the same routed review, not a separate unguarded action.
* Marks the submission `rejected` — terminal for this submission; the lock releases. Logged with the
  reason.
* The editor addresses the issue and runs `contract start` again on the same task, then `contract
  prepare` on the fresh submission it opens — a new lock, a new baseline, and a new submission, not a
  reopened one. This is the deliberate simplification that replaces verifier in-place editing (see
  `dish-task-contract-tool-future.md`, Dropped): rather than tracking who authored which exact bytes
  across a reassignment, a rejected note simply goes back through `start`/`prepare` as any other edit
  would.
* v1a applies no automatic lockout after repeated rejections on the same task; v2's two-pass-stop gate
  is designed once v1a's rejection-rate logging shows whether it's actually needed.

### 4. `contract submit`

```text
contract submit <submission-id> --file <same-final-note>
```

1. Loads the submission; requires status `ready`.
2. Rejects outright, with no Asana call attempted, if `write_count` is already 3 — a 4th write requires
   `contract-admin` to reset or replace the submission first.
3. Recomputes the file hash; rejects on any mismatch with `content_hash`.
4. Atomically flips status `ready` → `in_flight`.
5. Increments `write_count` — this happens for every attempt that reaches this point, i.e. every actual
   Asana mutation attempt, regardless of its outcome (success, confirmed failure, or uncertain). A
   failed `prepare` validation never reaches here and never increments it. There is no special case for
   a retry that resends identical content versus one that resends a genuinely different edit — both
   count the same way.
   * `write_count` reaches 1: no warning.
   * `write_count` reaches 2: succeeds, but logs a warning event ("stop doing tiny writes, one write
     remaining").
   * `write_count` reaches 3: succeeds, logged as the last allowed write for this submission.
6. Sends one complete notes update to Asana.
7. On clear success: marks `consumed` — the lock releases.
8. On confirmed API failure: reverts to `ready` — the same validated submission may be retried, up to
   the `write_count` limit above.
9. On an ambiguous/uncertain outcome: marks `uncertain` — resolved only by `contract-admin recover`
   (see Contract admin tool).

There is no pre-write freshness re-read here: `start` already holds an exclusive lock on the task for
this submission's entire life, so no other `contract` CLI caller can have moved `modified_at` in the
meantime (see Scope for the residual gap this doesn't cover — a direct edit made entirely outside the
guarded tool).

## Submission states

```text
drafting
awaiting_verification
ready
in_flight
consumed
uncertain
stale
rejected
```

Terminal (release the lock; do not block a new `start` on the same task): `consumed`, `stale`,
`rejected`.
Non-terminal (hold the lock; block a new `start` on the same task): `drafting`, `awaiting_verification`,
`ready`, `in_flight`, `uncertain`.

A `consumed`, `stale`, or `rejected` submission cannot be reused; a second submission attempt on the
same row fails even with identical content. A fresh `contract start` — a new row, a new lock, a new
baseline — is required after any terminal state.

## Failure behaviour

### Failure before mutation

Examples: deterministic validation failure; missing approval; content-hash mismatch; routing mismatch;
`write_count` already at its limit. No Asana write occurs. A wrong-file or hash-mismatch rejection does
not consume the submission and does not increment `write_count`, since it never reaches Asana.

### Confirmed API failure

When Asana clearly rejects the request and the tool knows the write was not applied, the submission
returns from `in_flight` to `ready`. The same validated submission may be retried after the cause is
addressed.

### Uncertain API outcome / crashed process

A timeout, lost response, or connection break moves the submission to `uncertain`. If the tool's own
process dies while a submission is `in_flight`, nothing recovers it automatically — no timeout,
background sweep, or automatic retry; the lock stays held and the task simply stays unavailable for a
new `start` until recovered. This does not block other tasks' submissions.

Recovery is `contract-admin recover <submission-id>` (Marco-only, not the agent-facing CLI — an agent
should not resolve an ambiguous write outcome itself). It performs one targeted read of live notes and
live `modified_at`, then applies this outcome table — notes state is the primary signal, `modified_at`
can only make the outcome stricter, never looser:

| Live notes match | Live `modified_at` vs. baseline | Outcome |
| --- | --- | --- |
| Intended final-content hash | unchanged or changed | `consumed` — the write applied; notes are the write's own effect regardless of incidental `modified_at` movement. |
| Original baseline-notes hash | unchanged | `ready` — consistent with a confirmed failed write; the same validated content may be retried. |
| Original baseline-notes hash | changed | `stale`, Marco-led recovery required — notes never changed, but something else touched the task after baseline capture. |
| Neither | unchanged or changed | `stale`, Marco-led recovery required. |

## Contract admin tool

Marco-only actions live in a separate `contract-admin` command surface — a distinct
binary/subcommand namespace, so the boundary is unambiguous at the command line rather than a naming
similarity to the agent-facing `contract` commands. This is an operational and social convention, not a
technical secret: this design document and the tool's own code are both agent-readable. The actual
boundary is that agents are not instructed or expected to look for or invoke it, consistent with the
"not adversarial security" framing in Scope.

`contract-admin` covers:

* `contract-admin recover <submission-id>` — resolve a stuck `in_flight` or `uncertain` submission
  after a process crash or an ambiguous Asana response;
* `contract-admin reset <submission-id>` — clear a submission that has hit its 3-write limit (see
  `contract submit`) so the task can be written again; the editor still starts over via a fresh
  `contract start`, this only releases the exhausted row's hold rather than reopening it for reuse;
* other Marco-only actions identified later, including v2's `contract-admin unblock` once the
  two-failed-pass gate is built.

Revoking a task's contract-managed status is not a feature of `contract-admin`, or of this design at
all. If a managed task genuinely needs a one-off manual edit outside the guarded workflow, Marco makes
it directly through the Asana web UI instead — the same documented bypass named in Scope: a direct edit
there isn't prevented, only caught reactively by `contract-admin recover`, not by any ongoing check. The
general-purpose Asana CLI
gives Marco no bypass either, once v1b's block is active — nothing about being Marco is authenticated
or distinguishable to it.

## SQLite model

### `submissions`

* `submission_id`
* `task_gid`
* `baseline_modified_at`
* `baseline_notes_hash`
* `contract_revision`
* `contract_text_hash`
* `canonical_manifest`
* `editor_agent`
* `editor_family`
* `change_level`
* `change_reason`
* `required_verifier_family` (null for `small`)
* `verifier_agent` (null until approved, or always null for `small`)
* `verifier_family`
* `content_hash`
* `status`
* `write_count` (default 0; incremented on every actual Asana mutation attempt at `submit`; see
  `contract submit`)
* `created_at` (set at `start`, when the row and its lock are first created)
* `approved_at`
* `completed_at`

A partial unique index on `submissions(task_gid)` for non-terminal `status` values (including
`drafting`) enforces at most one open submission — and thus at most one held lock — per task.

### `audit_events`

* `event_id`
* `submission_id` (nullable — an advisory bypass event from the generic CLI has no submission; a
  failed `prepare` validation does, since `start` already opened the row it attaches to)
* `task_gid` (populated whenever known, even without a submission)
* `event_type`
* `actor_agent`
* `details` (structured — e.g. the specific rules a validation failure tripped, or a rejection reason)
* `created_at`

SQLite transactions protect local state changes and prevent two local submissions from consuming the
same row.

## Logging and observability (v1a)

v1a exists to prove the mechanism and learn real usage before enforcing anything, so logging is a
first-class requirement, not an afterthought on top of `audit_events`.

Every `contract` command execution logs an event regardless of outcome:

* command name, timestamp, invoking agent, task GID (when applicable), submission ID (once one
  exists);
* full outcome: pass/fail, and on failure, every specific rule that failed — not just "validation
  failed" — so Marco can see which rules trip in practice and which never fire;
* for `prepare`: declared change level and reason, and whether the note passed validation;
* for `approve`/`reject`: verifier agent/family, the decision, and for `reject`, the stated reason, so
  rejection patterns are visible without reading every case individually;
* for `submit`: the resulting `write_count` and the final submission state, so the 2nd-write warning and
  3rd-write last-allowed notice are both visible in the log, not just returned to the caller;
* for `prepare`: the compact diff summary against the prior baseline (`characters_added`,
  `characters_removed`, `lines_changed`, `headings_touched` — see Workflow §2), so `small`-declared
  diffs can actually be characterized later, not just counted.

The generic Asana CLI's managed-task check also logs during v1a even though it does not yet block:
every note-write to a section-managed task made *outside* the guarded `contract` path is logged as an
advisory bypass event (task GID, command used, agent if known). This is the direct evidence for the
v1a-to-v1b decision — whether it's safe to flip the block on depends on how much real, legitimate
traffic would have been blocked, not a guess.

A periodic summary — a query over `audit_events`, not a new mechanism — should be able to answer at
minimum:

* how many `prepare`/`approve`/`reject`/`submit` calls happened, by agent and by change level;
* validation failure rate, broken down by which specific rule failed most often;
* rejection rate, and repeated-rejection-on-same-task rate — the input needed to decide whether v2's
  two-pass-stop rule is actually necessary;
* what real `small`-declared diffs actually touch and how large they are, from the diff summary logged
  at `prepare` (see Workflow §2) — the input needed to design v2's small-change speed bump;
* how many advisory bypass events occurred outside the guarded path, and on which tasks/agents;
* how often `write_count` reaches 2 or 3 in practice, and how often `contract-admin reset` is actually
  needed — the input needed to judge whether the 3-write limit is set at the right level.

## Content hashing

The validation binds to the exact content sent to Asana. Every hash comparison in this design —
`prepare`'s `content_hash`, `approve`'s match check, `submit`'s recomputation — uses the SHA-256 hash
of the same canonical UTF-8/LF bytes, never raw upload bytes. "Exact match" means equality of those
canonical hashes, not byte-for-byte equality of whatever was originally uploaded; if the tool
normalizes line endings or whitespace before hashing, that normalization is what "exact" is measured
against, consistently everywhere the design says "exact" or "byte-for-byte."

Initial canonicalization proposal: UTF-8 encoding; LF line endings; no trimming; no automatic
whitespace cleanup; no section reordering; no silent markdown rewriting; SHA-256; canonicalization
version stored with the record.

Before implementation — and confirmed by v1a's real usage before v1b enforces anything — this must be
tested against an Asana write/read round trip to determine whether Asana normalizes trailing newlines
or other note content, and which operations (comments, custom-field changes, section moves, etc.)
actually bump `modified_at`. An over-sensitive baseline would invalidate submissions on activity
unrelated to note content.

## Integration with the existing Asana CLI

The contract tool and general Asana CLI may share SDK client construction, task reads, task updates,
and error formatting. They must not share unguarded note-writing behaviour.

The general CLI consults the contract tool's live managed-task determination before changing notes —
advisory/logged in v1a, blocking in v1b (see `dish-task-contract-tool-future.md`, Versioning plan;
Contract-managed task registry, above).

The contract tool performs its final update through a separate guarded gateway that cannot be called
without a valid, `ready` submission.

## ChatGPT workflow

ChatGPT has no local CLI or SQLite access, so it cannot run any `contract` command itself.

Its output is one complete final-note file. That file must already include ChatGPT's own
`Self-verified: gpt, <date>` line, attested by ChatGPT as part of producing the note — the same
self-review requirement every other editor meets by writing the line itself. A local agent or Marco
does not add or backfill this line on ChatGPT's behalf: if it's missing, `contract prepare` fails
exactly as it would for any other editor's missing `Self-verified:` line, and the fix is a corrected
file from ChatGPT, not a local insertion.

A local agent or Marco then, declaring `--agent gpt` throughout — attributing the submission to
ChatGPT as editor even though a local process runs the commands on its behalf:

1. runs `contract start` on the task;
2. runs `contract prepare` with ChatGPT's file;
3. arranges `contract approve`/`reject` from the opposite (Claude) family per the standard routing
   rule — nothing ChatGPT-specific, since GPT and Codex are one family;
4. runs `contract submit` once approved.

ChatGPT cannot declare its own `--agent` value or run any command itself — a human or local agent does
so on its behalf, honestly reflecting who actually authored and reviewed the note.

## Testing requirements (v1a)

Implementation follows TDD. Tests must cover:

* SQLite schema and migrations;
* generic note-write advisory logging in v1a, and blocking once v1b is enabled;
* non-note generic writes remaining allowed in both v1a and v1b;
* declared agent-name validation and agent-family routing;
* small, medium, and large change-level handling; initial construction treated as large;
* verifier-family mismatch on `approve`;
* every deterministic-validation rule individually, including the readiness-contradiction rule;
* exact content-hash binding at `approve` and at `submit`;
* content changed after `prepare` (hash mismatch) rejected at `approve`, with no path for the verifier
  to submit edited content through `approve`;
* `contract reject` marking a submission terminal (lock released) and requiring a fresh `contract
  start`, not a reopened submission;
* `contract reject` rejecting a call from an agent whose family does not match
  `required_verifier_family`, exactly as `approve` does;
* concurrent `contract start` on a task with an already-open submission (including one still in
  `drafting`) rejected, both by application check and by the SQLite unique constraint — the lock;
* `contract prepare`/`approve`/`reject`/`submit` called against a nonexistent or wrong-status
  `submission-id` rejected;
* `start` capturing `baseline_modified_at`/`baseline_notes_hash` once, and `prepare` using that same
  captured baseline rather than re-reading the task;
* no Asana mutation on any pre-write failure; exactly one Asana mutation attempt per `submit` call that
  reaches the API;
* submission reuse rejection (`consumed`/`stale`/`rejected` cannot be resubmitted, even with identical
  content);
* two simultaneous `submit` calls on one submission;
* `write_count` escalation at `submit`: write 1 no warning, write 2 succeeds with a warning event
  logged, write 3 succeeds and is logged as the last allowed write, write 4 rejected before any Asana
  call is attempted;
* a submission at its `write_count` limit is unusable until `contract-admin reset`, and usable again
  immediately after;
* confirmed API failure preserving retry eligibility (`in_flight` → `ready`) and incrementing
  `write_count` for that attempt;
* uncertain outcome where the write succeeded, where it did not, and a third conflicting state, each
  resolved correctly by `contract-admin recover`'s outcome table;
* raw `notes`/`html_notes` bypass attempts;
* `Self-verified:` line missing, or naming an agent other than `editor_agent`, fails `prepare`;
* a ChatGPT-authored file missing its own `Self-verified: gpt, <date>` line fails `prepare`, and no
  local-agent insertion satisfies it;
* `editor_agent == verifier_agent` on `approve` is unreachable — always rejected by the family check
  first, confirming no separate collision path exists to test;
* the diff summary (`characters_added`/`characters_removed`/`lines_changed`/`headings_touched`) is
  logged at every `prepare` pass, without persisting a second full copy of the note;
* `approve`/`submit` hash comparisons use the canonical-byte hash consistently, not raw upload bytes
  (see Content hashing);
* `canonical_manifest` captured at `prepare` remains authoritative for the submission even if the
  governing contract text changes before `submit`;
* section-GID resolution: a `Sourcing`/`Reference` rename does not change managed status; an
  unresolvable section fails closed to managed;
* every command execution produces an `audit_events` row, including failed `prepare` attempts on an
  already-open (`drafting`) submission and advisory bypass events from the generic CLI, which has no
  submission at all;
* the periodic-summary queries listed in Logging and observability return correct counts against a
  seeded `audit_events` fixture.

## Out of scope (all versions)

* cryptographically authenticating agents;
* inferring change level from note text;
* deciding semantic culinary correctness;
* recursively auditing dependencies;
* governing non-note task fields;
* automatically migrating every existing dish task's content to the current canonical structure;
* modifying the contract text or incident logs;
* providing a remote or multi-user trust service;
* documenting the contract admin tool's location or invocation for agents;
* providing a dedicated revoke-management command (Marco uses the Asana web UI directly instead).

## Open decisions

1. **Initial management — resolved.** No explicit enrollment step; a task is contract-managed by
   default based on its current Cooking-project section, excluding only `Sourcing` and `Reference`
   (see Contract-managed task registry).
2. **Marco-only actions — resolved.** All Marco-only actions live in `contract-admin`, separate from
   and invisible to the agent-facing `contract` commands. Revoking managed status is not a feature of
   this design: the general Asana CLI is guarded identically to any other caller once v1b is active,
   and gives Marco no bypass; a one-off manual edit goes through the Asana web UI instead.
3. **Submission lifetime — resolved.** No automatic expiry. A stuck `in_flight`/`uncertain` submission
   requires an explicit `contract-admin recover` call; no background timeout or heartbeat-based
   auto-recovery.
4. **Verifier edits — resolved, and simplified from an earlier draft.** The verifier cannot submit
   edited content at all. A hash mismatch at `approve` is a hard reject with no override; the verifier
   must instead `reject` with a reason, and the editor runs `contract start` and `contract prepare`
   again with a corrected file as a fresh submission (see Workflow §3; `dish-task-contract-tool-future.md`,
   Dropped).
5. **Existing tasks — resolved.** Every pre-existing task in the Cooking project outside
   `Sourcing`/`Reference` is contract-managed immediately; whether existing tasks' *content* needs
   migration to the current canonical structure is a separate, out-of-scope question.

The small-change-carelessness question (Marco's standing concern about an honest agent mis-declaring a
material change as `small`) moved to `dish-task-contract-tool-future.md` — it's targeted for v2, not v1.
