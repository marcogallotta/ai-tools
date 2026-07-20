# Dish Task Contract Tool — Design Draft

**Purpose:** Provide one controlled path for validating and writing complete contract-governed
dish-task notes to Asana.

**Status:** Initial design, v1 scope only. No implementation or production changes are authorized by
this document. Everything not needed for v1 to exist and work — v1b's enforcement flip, v2 candidate
features, and ideas considered and rejected outright — lives in `dish-task-contract-tool-future.md`,
not here.

## Scope

This tool governs complete writes to the notes of contract-managed dish tasks.

It is separate from the general-purpose Asana CLI. The existing CLI must consult this tool's live
managed-task determination before performing a generic note mutation — during v1a that consultation
is advisory/log-only (see `dish-task-contract-tool-future.md`, Versioning plan); v1b makes it a hard
block.

This design does not attempt adversarial security. Agents are trusted to identify themselves and
describe their work honestly. Mechanical controls exist to prevent concurrent controlled
submissions, repeated writes, and incomplete validation.

Direct web or integration edits are not prevented and are not generally identifiable as bypasses.
`contract start` claims an exclusive lock; while it is held, no other `contract` CLI caller can
start work on the same task. V1 assumes no edits are made outside this controlled workflow during
the cycle. It does not hash candidate content, save a live-notes baseline, or detect external edits;
those protections may be reconsidered for V2 if usage justifies them.

## Current design decisions, pending formal approval

- Contract-managed notes cannot be changed through generic note-writing commands once v1b is
  enabled; in v1a the restriction is advisory/logged only.
- Agent identity is supplied explicitly as a trusted CLI flag, not cryptographically authenticated.
- Trusted state is stored in a local SQLite database at `~/ai-tools/var/dish-contract.db`,
  gitignored, shared by every locally-invoked agent regardless of family.
- `contract start` claims an exclusive per-task lock, held by one `submissions` row from `drafting`
  through any terminal state; the `submission_id` it creates is used as the token for every later
  command on that submission. A verifier return to construction stays inside the same submission and
  keeps the lock held — see Workflow.
- The editing agent declares a **change level** (`small`/`medium`/`large`) and a reason; Python does
  not infer it.
- Every note passed between workflow stages is complete — no patches or fragments.
- A successful write consumes a single-use submission; an identical second write is rejected.
- A submission gets exactly one write; a second `submit` attempt on an already-`consumed` submission
  is rejected (see `contract submit`). No incident evidences a need for a multi-write escalation
  budget or reset mechanism — see `dish-task-contract-tool-future.md` if v1a's logging shows
  otherwise.

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

`medium`/`large` require an opposite-family verifier to `approve` before submission. `small`
requires no verifier at all — confirmed only by deterministic validation, not by a second agent (see
Agent identity and verifier routing).

## Agent identity and verifier routing

Every command that introduces or checks an attribution (`start`, `prepare`, `approve`, `reject`)
requires:

```text
--agent claude|gpt|codex
```

`submit` does not take `--agent` — it operates on an already-fully-attributed `ready` submission and
adds no new attribution of its own, so there is nothing for the flag to declare.

`--agent` is trusted as an honest declaration. Agent families:

```text
claude          → Claude family
gpt, codex      → GPT family
```

This mirrors the contract's own family definition ("GPT includes ChatGPT/Codex"), so a
ChatGPT-authored submission routes the same as any other GPT-family edit. ChatGPT cannot run the CLI
itself; whoever runs `contract start`/`prepare`/`submit` on its behalf declares `--agent gpt`.

Initial construction and `large` changes require verification by the opposite family. `medium` also
requires the opposite family, but the verifier may focus review on the declared affected areas when
containment is accepted. `small` matches the contract's Local change class: no verifier runs, and
the task's existing `Verification` field is left as-is.

The opposite-family requirement on `approve` makes `editor_agent == verifier_agent` structurally
unreachable — it fails the family check before any further comparison would matter. The residual
risk of one session dishonestly declaring different agent values for editing and verification is not
detectable under the trusted-identity model (see Scope) and is not claimed to be caught here.

The final task process record must agree with the declared final editor, routing, change level, and
governing contract revision, but V1 does not establish that agreement by parsing field values. The
editor and verifier check it manually; trusted CLI/SQLite state controls workflow routing.

## Contract-managed task registry

Management is determined by the task's current section in the Cooking project (`1215089183018968`),
checked live rather than fixed once at enrollment. Sections are identified by their immutable Asana
section GID, not by display name — a section rename must not silently change which tasks are
managed. The tool resolves the `Sourcing` and `Reference` sections' GIDs once by name at setup time
and compares against those GIDs thereafter. A task is contract-managed unless its current section
GID matches one of those two — every other section defaults to managed. If section membership cannot
be resolved to a GID at all, the tool fails closed and treats the task as managed. This applies
uniformly to new and pre-existing tasks; no separate enrollment or backfill pass is needed.

**v1a:** the generic CLI still performs a live check before a note mutation on a managed task, but
only to log an advisory bypass event (task GID, command used, agent if known) — the write proceeds.
**v1b:** the same check rejects the write instead.

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
1. Confirms no other open submission already exists for this task — enforced by application check
   and by a partial unique index on `submissions(task_gid)` for non-terminal `status` (including
   `drafting`), so a race between two simultaneous `start` calls fails at the database layer, not
   only in application logic. This is the lock: two agents cannot both `start` the same task.
1. Creates one `submissions` row, status `drafting`. The row's `submission_id` is printed back to
   the caller and used as the token for every subsequent command
   (`prepare`/`approve`/`reject`/`submit`) on this submission — there is no separate token object.

The lock is held for as long as the submission stays in a non-terminal status, and releases
automatically when the submission reaches `consumed` — see Submission states. Returning a note to
construction does not release it.

### 2. `contract prepare`

```text
contract prepare <submission-id> \
  --agent claude|gpt|codex \
  --change-level small|medium|large \
  --change-reason "<reason>" \
  --file <candidate-note>
```

Before running this, the agent has already assembled one complete canonical note (no patches or
fragments accepted anywhere in this workflow) and performed its own end-to-end self-review, scoped
by change level the same way the contract already scopes verification — local check for `small`, the
change and its identified dependencies for `medium`, the complete task for `large`. It records that
review by writing `Self-verified: <agent>, <date>` into the file's process record; V1 checks that
the label exists, not the grammar or meaning of its value.

The tool:

1. Confirms the submission exists and is in status `drafting`.
1. Reads the exact governing contract text (from `$CONTRACT_MD_PATH`, defaulting to
   `~/honest-pantry/dish-task-contract.md`), including its embedded canonical-structure manifest,
   and stores the parsed manifest as `canonical_manifest` alongside `contract_revision`. This
   submission uses that frozen revision and manifest for its entire life; a later contract edit does
   not affect an already-open submission.
1. Runs deterministic validation (below) against `<candidate-note>`.
1. On a validation failure: reports every violated rule; the submission stays in `drafting` (it
   already exists, opened by `start`), but the attempt is logged (see Logging and observability) so
   failure patterns are visible even before a validation pass.
1. On a pass, advances the row out of `drafting`.
   - `small` → status `ready`, no verifier required.
   - `medium`/`large` → status `awaiting_verification`, with `required_verifier_family` set to the
     family opposite `editor_family`.

Deterministic validation checks literal template shape only:

- headings and labels that the canonical manifest marks required are present;
- headings and labels that must occur once occur exactly once;
- when `## QUANTITIES` is present, a `Portions:` label is present under it;
- no heading exists outside the manifest's allowlist.

Text after a label is opaque to V1. It does not parse or interpret portions, macros, readiness,
Planning exceptions, Human Review state, verification results, self-verification identity, contract
releases, change-level wording, or any other field value. Their correctness remains editor/verifier
work. Field grammar, tolerant schemas, and tool-generated values are V2 questions to design only
when a specific automation needs them.

The canonical allowlist is parsed from the manifest carried in the contract text itself, not
duplicated by hand in this tool — required as part of v1a's scope, not a later addition. A
hand-maintained hardcoded allowlist would recreate, inside the validator meant to eliminate this
exact failure mode, the same silent-drift risk the tool exists to remove from the contract's own
prose rules.

Deferred to V2 once the mechanical layer is proven: field grammar and value parsing; unresolved
structural placeholder detection (e.g. leftover `[approx]` markers); any judgment of whether an
omitted optional section should have been present; and content quality.

The validator performs no Asana mutation and does not decide whether the recipe is culinarily
correct, whether research is adequate, or whether the declared change level is semantically honest.

### 3. `contract approve` / `contract reject`

Required only for `medium`/`large`. The verifier reviews the prepared file for culinary and internal
consistency, evidence adequacy, readiness, editor/verifier routing, the declared change level, and
containment for a `medium` change. The verifier may make a clear correction, recheck the complete
file, and sign it; the tool does not classify whether that judgment was correct.

```text
contract approve <submission-id> --agent <verifier-agent> --file <final-note>
```

- Requires `verifier-agent`'s family to be the submission's `required_verifier_family`.
- Reruns literal template-shape validation on the verifier's complete final file. The verifier
  manually checks the signed `Verification:` value and frozen contract revision.
- On pass, records `verifier_agent`/`verifier_family` and sets status `ready`.

```text
contract reject <submission-id> --agent <verifier-agent> --reason "<why not signable>"
```

- Requires `verifier-agent`'s family to be the submission's `required_verifier_family`, exactly as
  `approve` does — rejection is part of the same routed review, not a separate unguarded action.
- Returns the submission to `drafting` and logs the reason. The lock remains held while the editor
  corrects the note and runs `prepare` again on the same submission; no new `start` is allowed.
- v1a applies no automatic lockout after repeated rejections on the same task; v2's two-pass-stop
  gate is designed once v1a's rejection-rate logging shows whether it's actually needed.

### 4. `contract submit`

```text
contract submit <submission-id> --file <final-note>
```

1. Loads the submission; requires status `ready`.
1. Trusts the controlled handoff to supply the final reviewed file; V1 does not bind it by hash or
   compare the live task with a saved baseline.
1. Atomically flips status `ready` → `in_flight`.
1. Sends one complete notes update to Asana.
1. On clear success: marks `consumed` — the lock releases. A submission is single-use: a second
   `submit` call against a `consumed` submission is rejected outright.
1. On confirmed API failure: reverts to `ready` — the same validated submission may be retried.
1. On an ambiguous/uncertain outcome: marks `uncertain` — logged for Marco to check directly in
   Asana (see Contract admin tool). No incident evidences a crash or ambiguous-outcome case in
   practice; a deterministic recovery table is a v2 candidate once real usage shows it's needed (see
   `dish-task-contract-tool-future.md`).

The local lock prevents concurrent contract-tool submissions. V1 explicitly accepts that it neither
prevents nor detects a web, integration, or generic-CLI edit made while that lock is held.

## Submission states

```text
drafting
awaiting_verification
ready
in_flight
consumed
uncertain
```

Terminal (release the lock; do not block a new `start` on the same task): `consumed`. Non-terminal
(hold the lock; block a new `start` on the same task): `drafting`, `awaiting_verification`, `ready`,
`in_flight`, `uncertain`.

A `consumed` submission cannot be reused. A fresh `contract start` is required for a later cycle.

## Failure behaviour

### Failure before mutation

Examples: deterministic validation failure, missing approval, or routing mismatch. No Asana write
occurs, and the submission stays locked in its existing state.

### Confirmed API failure

When Asana clearly rejects the request and the tool knows the write was not applied, the submission
returns from `in_flight` to `ready`. The same validated submission may be retried after the cause is
addressed.

### Uncertain API outcome / crashed process

A timeout, lost response, or connection break moves the submission to `uncertain`. If the tool's own
process dies while a submission is `in_flight`, nothing recovers it automatically — no timeout,
background sweep, or automatic retry; the lock stays held and the task simply stays unavailable for
a new `start` until recovered. This does not block other tasks' submissions.

No incident evidences a crashed process or an ambiguous Asana outcome happening in practice. v1a
logs the `uncertain` state and leaves recovery to Marco checking the live task directly and using
`contract-admin recover <submission-id>` (Marco-only) to set the submission's status by hand once
he's confirmed what actually happened. A deterministic outcome table driven off live
notes/`modified_at` comparison is a v2 candidate once real usage shows this needs to be automated
(see `dish-task-contract-tool-future.md`).

## Contract admin tool

Marco-only actions live in a separate `contract-admin` command surface — a distinct
binary/subcommand namespace, so the boundary is unambiguous at the command line rather than a naming
similarity to the agent-facing `contract` commands. This is an operational and social convention,
not a technical secret: this design document and the tool's own code are both agent-readable. The
actual boundary is that agents are not instructed or expected to look for or invoke it, consistent
with the "not adversarial security" framing in Scope.

`contract-admin` covers:

- `contract-admin recover <submission-id>` — set a stuck `in_flight` or `uncertain` submission's
  status by hand after Marco has checked the live task directly and confirmed what actually
  happened;
- other Marco-only actions identified later, including v2's `contract-admin unblock` once the
  two-failed-pass gate is built.

Revoking a task's contract-managed status is not a feature of `contract-admin`, or of this design at
all. If a managed task genuinely needs a one-off manual edit outside the guarded workflow, Marco
makes it directly through the Asana web UI instead — the same documented bypass named in Scope: a
direct edit there is neither prevented nor detected by V1. The general-purpose Asana CLI gives Marco
no bypass either, once v1b's block is active — nothing about being Marco is authenticated or
distinguishable to it.

## SQLite model

### `submissions`

- `submission_id`
- `task_gid`
- `contract_revision`
- `canonical_manifest`
- `editor_agent`
- `editor_family`
- `change_level`
- `change_reason`
- `required_verifier_family` (null for `small`)
- `verifier_agent` (null until approved, or always null for `small`)
- `verifier_family`
- `status`
- `created_at` (set at `start`, when the row and its lock are first created)
- `approved_at`
- `completed_at`

A partial unique index on `submissions(task_gid)` for non-terminal `status` values (including
`drafting`) enforces at most one open submission — and thus at most one held lock — per task.

### `audit_events`

- `event_id`
- `submission_id` (nullable — an advisory bypass event from the generic CLI has no submission; a
  failed `prepare` validation does, since `start` already opened the row it attaches to)
- `task_gid` (populated whenever known, even without a submission)
- `event_type`
- `actor_agent`
- `details` (structured — e.g. the specific rules a validation failure tripped, or a rejection
  reason)
- `created_at`

SQLite transactions protect local state changes and prevent two local submissions from consuming the
same row.

## Logging and observability (v1a)

v1a exists to prove the mechanism and learn real usage before enforcing anything, so logging is a
first-class requirement, not an afterthought on top of `audit_events`.

Every `contract` command execution logs an event regardless of outcome:

- command name, timestamp, invoking agent, task GID (when applicable), submission ID (once one
  exists);
- full outcome: pass/fail, and on failure, every specific rule that failed — not just "validation
  failed" — so Marco can see which rules trip in practice and which never fire;
- for `prepare`: declared change level and reason, and whether the note passed validation;
- for `approve`/`reject`: verifier agent/family, the decision, and for `reject`, the stated reason,
  so rejection patterns are visible without reading every case individually;
- for `submit`: the final submission state (`consumed`, reverted to `ready` on confirmed failure, or
  `uncertain`), so every outcome is visible in the log, not just returned to the caller.

The generic Asana CLI's managed-task check also logs during v1a even though it does not yet block:
every note-write to a section-managed task made *outside* the guarded `contract` path is logged as
an advisory bypass event (task GID, command used, agent if known). This is the direct evidence for
the v1a-to-v1b decision — whether it's safe to flip the block on depends on how much real,
legitimate traffic would have been blocked, not a guess.

A periodic summary — a query over `audit_events`, not a new mechanism — should be able to answer at
minimum:

- how many `prepare`/`approve`/`reject`/`submit` calls happened, by agent and by change level;
- validation failure rate, broken down by which specific rule failed most often;
- rejection rate, and repeated-rejection-on-same-task rate — the input needed to decide whether v2's
  two-pass-stop rule is actually necessary;
- how many advisory bypass events occurred outside the guarded path, and on which tasks/agents.

Further queries (small-change diff characterization, write/reset frequency) are v2 candidates, tied
to mechanisms not built in v1a either — see `dish-task-contract-tool-future.md`.

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

Its output is one complete candidate-note file. That file must already include ChatGPT's own
`Self-verified: gpt, <date>` line, attested by ChatGPT as part of producing the note — the same
self-review requirement every other editor meets by writing the line itself. A local agent or Marco
does not add or backfill this line on ChatGPT's behalf: if it's missing, `contract prepare` fails
exactly as it would for any other editor's missing `Self-verified:` line, and the fix is a corrected
file from ChatGPT, not a local insertion.

A local agent or Marco then, declaring `--agent gpt` throughout — attributing the submission to
ChatGPT as editor even though a local process runs the commands on its behalf:

1. runs `contract start` on the task;
1. runs `contract prepare` with ChatGPT's file;
1. arranges `contract approve`/`reject` from the opposite (Claude) family per the standard routing
   rule — nothing ChatGPT-specific, since GPT and Codex are one family;
1. runs `contract submit` once approved.

ChatGPT cannot declare its own `--agent` value or run any command itself — a human or local agent
does so on its behalf, honestly reflecting who actually authored and reviewed the note.

## Testing requirements (v1a)

Implementation follows TDD. Tests must cover:

- SQLite schema and migrations;
- generic note-write advisory logging in v1a, and blocking once v1b is enabled;
- non-note generic writes remaining allowed in both v1a and v1b;
- declared agent-name validation and agent-family routing;
- small, medium, and large change-level handling; initial construction treated as large;
- verifier-family mismatch on `approve`;
- every literal template-shape rule individually, including missing, duplicate, and unknown
  headings/labels, without interpreting their values;
- verifier-authored clear corrections accepted by `approve`, with deterministic validation rerun on
  the complete corrected file;
- `contract reject` returning the submission to `drafting` while retaining the lock, followed by a
  corrected `prepare` on that same submission;
- `contract reject` rejecting a call from an agent whose family does not match
  `required_verifier_family`, exactly as `approve` does;
- concurrent `contract start` on a task with an already-open submission (including one still in
  `drafting`) rejected, both by application check and by the SQLite unique constraint — the lock;
- `contract prepare`/`approve`/`reject`/`submit` called against a nonexistent or wrong-status
  `submission-id` rejected;
- no Asana mutation on any pre-write failure; exactly one Asana mutation attempt per `submit` call
  that reaches the API;
- submission reuse rejection after `consumed`;
- two simultaneous `submit` calls on one submission;
- a submission is single-use: a second `submit` call against an already-`consumed` submission is
  rejected;
- confirmed API failure preserving retry eligibility (`in_flight` → `ready`);
- an uncertain `submit` outcome is logged as `uncertain` and left for Marco to resolve via
  `contract-admin recover`, without asserting any automatic outcome-table behaviour;
- raw `notes`/`html_notes` bypass attempts;
- a missing `Self-verified:` label fails `prepare`; V1 does not parse its value;
- a ChatGPT-authored file missing the `Self-verified:` label fails `prepare`, and no local-agent
  insertion satisfies the authorship requirement even though V1 cannot verify that judgment;
- `editor_agent == verifier_agent` on `approve` is unreachable — always rejected by the family check
  first, confirming no separate collision path exists to test;
- `canonical_manifest` captured at `prepare` remains authoritative for the submission even if the
  governing contract text changes before `submit`;
- section-GID resolution: a `Sourcing`/`Reference` rename does not change managed status; an
  unresolvable section fails closed to managed;
- every command execution produces an `audit_events` row, including failed `prepare` attempts on an
  already-open (`drafting`) submission and advisory bypass events from the generic CLI, which has no
  submission at all;
- the periodic-summary queries listed in Logging and observability return correct counts against a
  seeded `audit_events` fixture.

## Out of scope (all versions)

- cryptographically authenticating agents;
- inferring change level from note text;
- deciding semantic culinary correctness;
- recursively auditing dependencies;
- governing non-note task fields;
- automatically migrating every existing dish task's content to the current canonical structure;
- modifying the contract text or incident logs;
- providing a remote or multi-user trust service;
- documenting the contract admin tool's location or invocation for agents;
- providing a dedicated revoke-management command (Marco uses the Asana web UI directly instead);
- a speed bump against an honest agent carelessly mis-declaring a material change as `small`
  (Marco's standing concern) — tracked in `dish-task-contract-tool-future.md` for v2, once v1a's
  logging of what real `small`-declared diffs touch gives the input needed to design it.
