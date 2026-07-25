# Dish Tool — Design Draft

**Purpose:** Provide one controlled interface for creating, reading, validating, writing, and moving
protocol-governed dish tasks. Asana is the initial backend, not part of the agent-facing workflow.

**Status:** Initial design, v1 scope only. No implementation or production changes are authorized by
this document. Everything not needed for v1 to exist and work — v1b's enforcement flip, v2 candidate
features, and ideas considered and rejected outright — lives in `dish-tool-future.md`, not here.

V1 ships only after the tool-independent three-way protocol split is live. A later tool-aware beta
of those three protocols supplies the command-facing workflow and machine-readable manifest used
here; the current beta remains intentionally usable without this tool and is not retrofitted during
tool implementation.

## Scope

This tool governs the agent-facing lifecycle of protocol-managed dish tasks: task creation and
reads, complete notes writes, and the two conditional queue moves. Agents using the tool-aware
protocols do not call the generic Asana CLI or depend on Asana-specific concepts.

It is separate from the general-purpose Asana CLI. The existing CLI must consult this tool's live
managed-task determination before performing a generic note mutation — during v1a that consultation
is advisory/log-only (see `dish-tool-future.md`, Versioning plan); v1b makes it a hard block.

This design does not attempt adversarial security. Agents are trusted to identify themselves and
describe their work honestly. Mechanical controls exist to prevent concurrent controlled
submissions, repeated writes, and incomplete validation.

Direct web or integration edits are not prevented and are not generally identifiable as bypasses.
`dish start` claims an exclusive lock; while it is held, no other `dish` CLI caller can start work
on the same task. V1 assumes no edits are made outside this controlled workflow during the cycle. It
does not hash candidate content, save a live-notes baseline, or detect external edits; those
protections may be reconsidered for V2 if usage justifies them.

## Current design decisions, pending formal approval

- Protocol-managed notes cannot be changed through generic note-writing commands once v1b is
  enabled; in v1a the restriction is advisory/logged only.
- Agent identity is supplied explicitly as a trusted CLI flag, not cryptographically authenticated.
- Trusted state is stored in a local SQLite database at `~/ai-tools/var/dish-tool.db`, gitignored,
  shared by every locally-invoked agent regardless of family.
- `dish start` claims an exclusive per-task lock, held by one `submissions` row from `drafting`
  through any terminal state; the `submission_id` it creates is used as the token for every later
  command on that submission. A verifier return to construction stays inside the same submission and
  keeps the lock held — see Workflow.
- Every submission declares a kind: `planning`, `initial`, or `change`. A `change` also declares a
  level (`small`/`large`) and reason; Python does not infer either value.
- Every note passed between workflow stages is complete — no patches or fragments.
- Every command returns one stable JSON result with a machine-readable outcome code, current state,
  retryability, and legal next agent actions; agents do not scrape human-oriented prose.
- The tool owns task creation and both queue transitions. Multi-step operations record completed
  steps and are safely retryable without repeating a notes write or making a contradictory move.
- Planning and complete-task manifests require one `Exemptions:` field. V1 narrowly parses its
  literal nutrition tags and preserves their set across the planning handoff; other field values
  remain opaque.
- A successful write consumes a single-use submission; an identical second write is rejected.
- A submission gets exactly one notes write; a second `submit` attempt on an already-`consumed`
  submission is rejected (see `dish submit`). No incident evidences a need for a multi-write
  escalation budget or reset mechanism — see `dish-tool-future.md` if v1a's logging shows otherwise.

## Submission kinds and change levels

`planning` writes the compact Planning brief into a bare task. It receives only deterministic
planning-template checks: no self-verification attestation and no opposite-family verification.
`initial` constructs the first complete researched task and receives whole-task opposite-family
verification. `change` covers post-construction work and requires a change level.

The tool-aware protocols use these same `small` and `large` terms; V1 does not carry forward the
monolithic protocol's Local/Delta/Reconstruction vocabulary.

### Small change

A change that cannot materially alter cooking, sourcing, safety, halal compliance, readiness, Human
approval, or the intended result. Examples may include spelling, formatting, or an unambiguous
correction with no material downstream effect.

### Large change

A material change. Initial construction uses the separate `initial` kind; it routes like a large
change but creates no post-construction Material changes entry.

At `start`, the editing agent declares either:

```text
--kind planning|initial
--kind change --change-level small|large --change-reason "<brief explanation>"
```

`large` requires an opposite-family verifier to `approve` before submission. `small` requires no
verifier at all — confirmed only by deterministic validation, not by a second agent (see Agent
identity and verifier routing).

## Agent identity and verifier routing

Every command that introduces or checks an attribution (`create`, `read`, `inspect`, `start`,
`prepare`, `approve`, `reject`) requires:

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

This mirrors the protocol's own family definition ("GPT includes ChatGPT/Codex"), so a
ChatGPT-authored submission routes the same as any other GPT-family edit. ChatGPT cannot run the CLI
itself; whoever runs `dish start`/`prepare` on its behalf declares `--agent gpt`; `submit` uses that
recorded editor attribution and takes no agent flag.

Planning receives scripted validation only. Initial construction and `large` changes require
verification by the opposite family. `small` requires no verifier, and the task's existing
`Verification` field is left as-is.

The opposite-family requirement on `approve` makes `editor_agent == verifier_agent` structurally
unreachable — it fails the family check before any further comparison would matter. The residual
risk of one session dishonestly declaring different agent values for editing and verification is not
detectable under the trusted-identity model (see Scope) and is not claimed to be caught here.

Trusted submission/audit state records the kind, any change level, editor, routing, and governing
protocol release. Initial construction has initial Verification but no post-construction Material
changes entry; a `small` change preserves the existing signed `Verification:` line and needs no
formal change entry; a `large` change records the material change and resets Verification. V1 does
not parse these field values, except for the narrow exact-line preservation check on `small`
submissions described below.

## CLI result contract

Every `dish` and `dish-admin` invocation, including argument-parsing and startup failures, writes
exactly one JSON object to stdout. Incidental diagnostics go to stderr and are never needed to
interpret the result. The common envelope is:

```json
{
  "ok": false,
  "command": "prepare",
  "code": "VALIDATION_FAILED",
  "task_gid": "...",
  "submission_id": "...",
  "state": "drafting",
  "retryable": true,
  "allowed_actions": ["prepare"],
  "data": {},
  "errors": [{"rule": "missing_label", "field": "Exemptions"}]
}
```

Fields that do not apply are `null` or empty, never omitted. `data` carries command-specific
results, including task content, the frozen protocol bundle, or created IDs. `allowed_actions`
lists only agent-facing `dish` commands legal from the returned state; a state requiring Marco does
not expose `dish-admin` invocation details and instead returns `HUMAN_ACTION_REQUIRED` with an empty
list.

Successful commands use `code: "OK"`. `inspect` is always permitted and therefore omitted from
`allowed_actions`, which describes the next state-changing step: `drafting` → `prepare`,
`research_handoff` → `prepare` (move-only retry), `awaiting_verification` → `approve` or `reject`,
`ready` → `submit`, and `written` → `submit` (move-only retry). `awaiting_human`, `in_flight`,
`uncertain`, `consumed`, and `discarded` have no legal agent state-changing action.

Stable top-level failure codes are `INVALID_ARGUMENT`, `NOT_FOUND`, `UNMANAGED_TASK`,
`VALIDATION_FAILED`, `WRONG_STATE`, `AGENT_MISMATCH`, `VERIFIER_FAMILY_MISMATCH`, `CONFLICT`,
`BACKEND_REJECTED`, `BACKEND_UNCERTAIN`, `HUMAN_ACTION_REQUIRED`, and `INTERNAL_ERROR`.
Rule-level validation identifiers live in `errors`; prose may explain a failure but never identifies
it by itself. Exit status is `0` for success, `2` for caller/input/validation failures, `3` for
state/routing/conflict failures, `4` for confirmed backend rejection, `5` for uncertain backend
outcomes, and `1` for unexpected internal failure. Tests, protocols, and callers rely on the JSON
codes rather than wording.

## Protocol-managed task registry

Management is determined by the task's current section in the Cooking project (`1215089183018968`),
checked live rather than fixed once at enrollment. Sections are identified by their immutable Asana
section GID, not by display name — a section rename must not silently change which tasks are
managed. The tool resolves the `Sourcing` and `Reference` sections' GIDs once by name at setup time
and compares against those GIDs thereafter. A task is protocol-managed unless its current section
GID matches one of those two — every other section defaults to managed. If section membership cannot
be resolved to a GID at all, the tool fails closed and treats the task as managed. This applies
uniformly to new and pre-existing tasks; no separate enrollment or backfill pass is needed.

**v1a:** the generic CLI still performs a live check before a note mutation on a managed task, but
only to log an advisory bypass event (task GID, command used, agent if known) — the write proceeds.
**v1b:** the same check rejects the write instead.

The check applies to `set-notes`, `append`, `replace`, batch operations updating notes, `raw` writes
containing `notes`/`html_notes`, and `create_task` when it supplies notes for a task whose intended
Cooking section is managed. An unresolved intended section fails closed to managed. Generic creation
of a bare managed task remains allowed during the rollout, but tool-aware agents use `dish create`.
Unrelated operations (rename, complete, other fields) remain outside this protocol unless later
expanded. Generic section moves remain available, but tool-aware agents use only the conditional
moves owned by `dish prepare` and `dish submit`.

The dish-tool submission path uses an internal guarded write operation after all checks pass; it
does not go through the generic-command guard at all, in either v1a or v1b.

## Workflow

`dish create` creates a bare task in Research Queue and returns its task GID. Planning immediately
opens a `planning` submission and writes its canonical Planning brief through the guarded path.
Research starts a separate `initial` submission from that live brief and prepares the complete
canonical task. This is the only V1 path from a bare task to a researched task; generic
`create_task` with notes cannot bypass it.

After `prepare` accepts completed Research for verification, the tool conditionally moves a task
from Research Queue to Verification Queue. Acceptance alone does not move it onward. After the final
notes write succeeds, `submit` conditionally moves a task from Verification Queue to the Planning
brief's validated Destination section. Rejection and failed or uncertain writes leave it in
Verification Queue. A task already outside both queues is a manual override and is never moved
automatically.

Each multi-step command inspects both recorded and live backend state. A retry completes only a
missing step: it never repeats a confirmed notes write or repeats or reverses a completed move.

### Protocol release resolver

V1 uses one resolver for the checked-in release manifest. A human-readable
`protocol_release` identifies the exact `dish-planning-protocol.md`, `dish-research-protocol.md`,
`dish-verification-protocol.md`, and canonical manifest/schema set at the Git commit that introduced
that release. The resolver loads those committed contents by role and fails closed if the release is
missing, ambiguous, incomplete, or the protocol set has uncommitted changes. Git provides the
exact-content binding; no combined hash is exposed in tasks.

### 0. `dish create` / `dish read` / `dish inspect`

```text
dish create --agent claude|gpt|codex --title "<working task title>"
dish read <task-gid> --agent claude|gpt|codex
dish inspect <submission-id> --agent claude|gpt|codex
```

Creates one bare task in the Cooking project's Research Queue and returns its task GID in `data`. It
performs no notes write. A clear API failure returns `BACKEND_REJECTED`; an ambiguous outcome returns
`BACKEND_UNCERTAIN` for Marco to resolve rather than automatically retrying and risking a duplicate
task.

`dish read` returns the complete current task through the backend abstraction. It accepts any
Cooking task, including an excluded Reference or Sourcing task, because reading does not mutate or
enrol it. Tool-aware agents do not use the generic Asana CLI to fetch task content.

`dish inspect` returns the submission kind, current state, attribution and required verifier family,
frozen release and exact protocol/manifest bundle, recorded destination name/GID, completion
markers, and legal next agent actions. It is read-only and works in every state, including terminal
states. V1's candidate remains the explicitly controlled file handoff: `inspect` does not store or
return candidate bytes, invent a candidate path, or weaken the requirement that the verifier receive
the complete file being reviewed.

### 1. `dish start`

```text
dish start <task-gid> --agent claude|gpt|codex --kind planning|initial
dish start <task-gid> --agent claude|gpt|codex --kind change \
  --change-level small|large --change-reason "<reason>"
```

Claims the exclusive lock on the task and opens the submission that every later command in this
workflow operates on.

1. Confirms the task exists in the Cooking project and is protocol-managed.
1. Confirms the kind is valid, and that change level/reason are present only and always for
   `change`. A `planning` submission requires empty notes; `initial` requires a structurally valid
   Planning brief; `change` requires a structurally valid complete task.
1. Confirms no other open submission already exists for this task — enforced by application check
   and by a partial unique index on `submissions(task_gid)` for non-terminal `status` (including
   `drafting`), so a race between two simultaneous `start` calls fails at the database layer, not
   only in application logic. This is the lock: two agents cannot both `start` the same task.
1. Resolves the current checked-in `protocol_release`, loads the exact role-specific protocol and
   manifest set, and stores the release and Git binding. This frozen bundle governs authorship,
   self-review where required, validation, and verification for the submission's entire life.
   `start` returns the release and exact documents the author must read in `data` before drafting.
1. For a `small` change, captures the existing `Verification:` line for exact preservation.
1. For `initial` or `change`, captures the live Planning brief's normalized exemption-tag set for
   preservation through `prepare` and `approve`; a missing or malformed field makes `start` fail.
1. Creates one `submissions` row, status `drafting`. The row's `submission_id` is returned in `data`
   and used as the token for every subsequent command
   (`prepare`/`approve`/`reject`/`submit`) on this submission — there is no separate token object.

The lock is held for as long as the submission stays in a non-terminal status, and releases
automatically when the submission reaches `consumed` — see Submission states. Returning a note to
construction does not release it.

### 2. `dish prepare`

```text
dish prepare <submission-id> \
  --agent claude|gpt|codex \
  --file <candidate-note> \
  [--exemption-revision "<Marco decision, date, and reason>"]
```

For `planning`, the candidate is the complete compact Planning brief and receives scripted checks
only. For `initial` or `change`, the agent has assembled one complete canonical task and performed
the protocol's required self-review. It records that review in `Self-verified:`; V1 checks that the
label exists, not its value.

The tool:

1. Confirms the submission is `drafting` for validation or `research_handoff` for a move-only retry.
1. Requires `--agent` to match the submission's recorded `editor_agent`. A verifier taking
   ownership of a material correction must first do so through `reject --take-ownership`.
1. Reuses the protocol release and manifests frozen at `start`; it never resolves the current
   release again.
1. Runs the appropriate deterministic planning-brief or complete-task validation against
   `<candidate-note>`. A `small` change additionally requires its `Verification:` line to match the
   line captured at `start` byte-for-byte. The current release governs `small` classification,
   self-review, and structural checks without attributing the edit to the prior signer. If the old
   task cannot satisfy the current structure, it requires explicit migration rather than a `small`
   edit.
1. Parses `Exemptions:` as either `None` or a unique set of `[nutrition-kcal]`,
   `[nutrition-protein]`, and `[nutrition-fat]` followed by a non-empty scope/reason/approval note.
   Unknown tags, duplicate tags, mixed `None` and tags, or missing explanatory text fail. For
   `initial` or `change`, the normalized tag set must match the live Planning brief captured at
   `start`. A changed set is rejected for `small`; otherwise it requires `--exemption-revision`,
   which is stored in trusted audit state and must also be supported by the candidate's recorded
   Human decision. V1 verifies the syntax and trusted declaration, not whether Marco truly approved
   it. The flag is rejected for `planning`, `small`, or an unchanged tag set.
1. Parses the Planning brief's `Destination section` as a section name plus GID, resolves it live,
   and requires a non-queue section in the Cooking project. The validated name and resolved GID are
   stored for approval and the eventual conditional move; this narrow operational field is parsed
   even though other field values remain opaque.
1. On a validation failure: reports every violated rule; the submission stays in `drafting` (it
   already exists, opened by `start`), but the attempt is logged (see Logging and observability) so
   failure patterns are visible even before a validation pass.
1. On a pass, advances the row out of `drafting`.
   - `planning` or `small` change → status `ready`, no verifier required.
   - `initial` or `large` → status `research_handoff`, with `required_verifier_family` set to the
     family opposite `editor_family`; for a task currently in Research Queue, the command moves it
     to Verification Queue. A task already there needs no move, and a task outside both queues is
     left in place as a manual override. After recording completion it advances to
     `awaiting_verification`. A retry from `research_handoff` completes only the missing move.

Deterministic validation checks literal template shape plus the narrow exemption grammar. For
planning, it checks the Planning brief heading and required/exact-once labels from the planning
manifest. For a complete task:

- headings and labels that the canonical manifest marks required are present;
- headings and labels that must occur once occur exactly once;
- when `## QUANTITIES` is present, a `Portions:` label is present under it;
- no heading exists outside the manifest's allowlist.

Except for the exact `small`-change `Verification:` line and the narrow `Exemptions:` grammar above, text
after a label is opaque to V1. It does not parse or interpret portions, macros, readiness, Human
Review state, verification results, self-verification identity, protocol releases, change-level
wording, or any other field value. Their correctness remains editor/verifier work. Further field
grammar, tolerant schemas, and tool-generated values are V2 questions to design only when a specific
automation needs them.

The canonical allowlist is parsed from the machine-readable manifest in the frozen protocol release
set, not duplicated by hand in this tool — required as part of v1a's scope, not a later addition. A
hand-maintained hardcoded allowlist would recreate, inside the validator meant to eliminate this
exact failure mode, the same silent-drift risk the tool exists to remove from the protocol's own
prose rules.

Deferred to V2 once the mechanical layer is proven: further field grammar and value parsing;
unresolved structural placeholder detection (e.g. leftover `[approx]` markers); any judgment of
whether an omitted optional section should have been present; and content quality.

The validator performs no Asana mutation and does not decide whether the recipe is culinarily
correct, whether research is adequate, or whether the declared change level is semantically honest.

### 3. `dish approve` / `dish reject`

Required only for `initial` or `large`. The verifier reviews the prepared file for culinary and
internal consistency, evidence adequacy, readiness, editor/verifier routing, and the declared change
level. The verifier may make a clear correction, recheck the complete file, and sign it; the tool
trusts the declared correction level rather than inferring whether that judgment was correct.

```text
dish approve <submission-id> --agent <verifier-agent> --file <final-note> \
  --correction none|small
```

- Requires `verifier-agent`'s family to be the submission's `required_verifier_family`.
- `--correction small` declares a non-material verifier correction that may be rechecked and signed
  in the same pass. A material verifier correction cannot be approved in place.
- Reruns template-shape, exemption-tag, and Destination validation on the verifier's complete final
  file. Its normalized exemption set must equal the prepared set, and its live-resolved Destination
  name/GID must exactly equal the pair accepted by `prepare`. A verifier cannot introduce either
  change during `approve`. A Destination mismatch returns the submission to `drafting` for a fresh
  `prepare`, without counting as a failed verification pass; an exemption revision follows the
  ordinary rejection/new-`prepare` path. The verifier manually checks the signed `Verification:`
  value, frozen protocol release, exemption scope, and truth of the recorded Human approval.
- A Destination mismatch returns `VALIDATION_FAILED`, records the mismatch in the audit event, and
  leaves `prepare` as the sole legal next state-changing action.
- On pass, records `verifier_agent`/`verifier_family` and sets status `ready`. Verification
  acceptance does not move the task out of Verification Queue.

```text
dish reject <submission-id> --agent <verifier-agent> --reason "<why not signable>" \
  [--changed-since-prior "<what materially changed>"] [--take-ownership]
```

- Requires `verifier-agent`'s family to be the submission's `required_verifier_family`, exactly as
  `approve` does — rejection is part of the same routed review, not a separate unguarded action.
- `--take-ownership` declares that the verifier made or will make a material correction. The tool
  records that verifier as the new editor, so the next successful `prepare` routes verification to
  the opposite family. Without it, editor attribution and routing remain unchanged.
- The first rejection in the current review cycle returns the submission to `drafting` and logs the
  reason. The lock remains held while the editor corrects the note and runs `prepare` again on the
  same submission; no new `start` is allowed.
- The second rejection transitions to `awaiting_human`. Further `prepare`, `approve`, and `reject`
  calls are blocked. It requires `--changed-since-prior`; the escalation event combines that with
  both rejection reasons so Marco can see the remaining issue, what changed, why both passes failed,
  and what must concretely change.
- `dish-admin unblock` returns an `awaiting_human` submission to `drafting` only after Marco records
  the changed evidence, premise, method, or scope. It resets the consecutive-failed-pass counter for
  the reopened cycle without erasing the audit history. Marco's action is the gate because the stop
  exists to end repeated verification cycling: an agent that could clear it by writing its own reset
  record would turn the stop back into the loop it prevents. Do not relax this to a self-clear.

### 4. `dish submit`

```text
dish submit <submission-id> --file <final-note>
```

1. Loads the submission; requires status `ready` for a notes write or `written` for a move-only
   retry.
1. Trusts the controlled handoff to supply the final reviewed file; V1 does not bind it by hash or
   compare the live task with a saved baseline.
1. Creates a unique write-attempt ID and atomically flips status `ready` → `in_flight`, recording
   the attempt ID, timestamp, hostname, PID, and process-start token before any request can be sent.
1. Sends one complete notes update to Asana.
1. On clear write success: marks `written`, so a retry cannot repeat the notes write. Every
   post-request transition is conditional on the same attempt ID, so a stale process cannot commit
   state for an attempt that Marco has recovered.
1. If the task is currently in Verification Queue, moves it to the validated Destination section;
   if it is already there, the move is complete; if it remains in Research Queue for a planning
   submission, leaves it there; if it is outside both queues, preserves the manual override. A
   failed move leaves the submission `written` for a move-only retry.
1. After any required move succeeds: marks `consumed` — the lock releases. A submission is
   single-use: a second `submit` call against a `consumed` submission is rejected outright.
1. On confirmed non-application: reverts to `ready` — the same validated submission may be retried.
1. On an ambiguous/uncertain outcome: marks `uncertain` — logged for Marco to check directly in
   Asana (see Dish admin tool).

The client tracks whether request transmission may have begun. Only a local failure proven to occur
before any request bytes were sent, or a well-formed backend response explicitly rejecting the
mutation, is confirmed non-application. A timeout after sending may have begun, connection reset or
lost response, HTTP 5xx, malformed or undecodable response, cancellation after sending may have
begun, or SDK exception whose send phase is unknown is `uncertain`. A valid success response is the
only confirmed success. When the SDK cannot prove the narrower classification, it chooses
`uncertain`.

The local lock prevents concurrent dish-tool submissions. V1 explicitly accepts that it neither
prevents nor detects a web, integration, or generic-CLI edit made while that lock is held.

## Submission states

```text
drafting
research_handoff
awaiting_verification
awaiting_human
ready
in_flight
written
consumed
discarded
uncertain
```

Terminal (release the lock; do not block a new `start` on the same task): `consumed`, `discarded`.
Non-terminal (hold the lock; block a new `start` on the same task): `drafting`,
`research_handoff`, `awaiting_verification`, `awaiting_human`, `ready`, `in_flight`, `written`,
`uncertain`.

A `consumed` submission cannot be reused. A fresh `dish start` is required for a later cycle.

## Failure behaviour

### Failure before mutation

Examples: deterministic validation failure, missing approval, or routing mismatch. No Asana write
occurs, and the submission stays locked in its existing state.

### Confirmed non-application

When Asana clearly rejects the request and the tool knows the write was not applied, the submission
returns from `in_flight` to `ready`. The same validated submission may be retried after the cause is
addressed.

### Failure after confirmed notes write

Once the notes write is confirmed, the submission is `written`. A destination lookup or move
failure leaves it there; retrying `submit` performs only the missing conditional move and then marks
the submission `consumed`. It never writes the notes again.

### Uncertain API outcome / crashed process

A timeout, lost response, connection break, HTTP 5xx, or undecodable response after transmission may
have begun moves the submission to `uncertain`. If the tool's own process dies while a submission is
`in_flight`, nothing recovers it automatically — no timeout, background sweep, or automatic retry;
the lock stays held and the task simply stays unavailable for a new `start` until recovered. This
does not block other tasks' submissions.

No incident evidences a crashed process or an ambiguous Asana outcome happening in practice. v1a
logs the `uncertain` state and leaves recovery to Marco checking the live task directly. Recovery is
refused while the recorded process identity is still live or until a fixed quarantine interval has
elapsed; that interval must exceed the client's maximum request lifetime plus a documented safety
margin. Once both checks pass, `dish-admin recover <submission-id>` (Marco-only) records Marco's
inspected outcome and the concrete reason for it. A deterministic outcome table driven off live
notes/`modified_at` comparison remains a v2 candidate (see `dish-tool-future.md`).

## Dish admin tool

Marco-only actions live in a separate `dish-admin` command surface — a distinct binary/subcommand
namespace, so the boundary is unambiguous at the command line rather than a naming similarity to the
agent-facing `dish` commands. This is an operational and social convention, not a technical secret:
this design document and the tool's own code are both agent-readable. The actual boundary is that
agents are not instructed or expected to look for or invoke it, consistent with the "not adversarial
security" framing in Scope.

`dish-admin` covers:

- `dish-admin recover <submission-id> --outcome not-applied|applied --reason "<inspection>"` — after
  Marco checks the live task, set a stuck `in_flight` or `uncertain` submission to `ready` when the
  notes were not applied, or `written` when they were. It first requires the recorded process to be
  dead and the recovery quarantine interval to have elapsed, then invalidates the old attempt ID in
  the same transaction as the state change. Retrying `submit` from `written` completes only the
  destination move;
- `dish-admin discard <submission-id> --reason "<reason>"` — mark an abandoned `drafting`,
  `research_handoff`, `awaiting_verification`, `awaiting_human`, `ready`, or `written` submission
  `discarded`, release its lock, and log the reason
  without mutating or changing the lifecycle state of the Asana task. It rejects `in_flight`,
  `uncertain`, and terminal submissions and never runs automatically;
- `dish-admin unblock <submission-id> --reason "<concrete change>"` — return an `awaiting_human`
  submission to `drafting` after Human Review records the changed evidence, premise, method, or
  scope; reset the consecutive-failed-pass counter and retain the full audit history.

Revoking a task's protocol-managed status is not a feature of `dish-admin`, or of this design at
all. If a managed task genuinely needs a one-off manual edit outside the guarded workflow, Marco
makes it directly through the Asana web UI instead — the same documented bypass named in Scope: a
direct edit there is neither prevented nor detected by V1. The general-purpose Asana CLI gives Marco
no bypass either, once v1b's block is active — nothing about being Marco is authenticated or
distinguishable to it.

## SQLite model

### `submissions`

- `submission_id`
- `task_gid`
- `submission_kind` (`planning`, `initial`, or `change`)
- `protocol_release`
- `release_commit`
- `protocol_bundle` (the exact role-specific checked-in protocol contents frozen at `start`)
- `canonical_manifest` (planning or complete-task manifest, frozen at `start`)
- `baseline_exemption_tags` (normalized set captured from the live Planning brief; null for
  `planning`)
- `prepared_exemption_tags` (normalized set accepted at the latest successful `prepare`)
- `destination_section_name` (live-validated at the latest successful `prepare`)
- `destination_section_gid` (live-validated at the latest successful `prepare`)
- `exemption_revision` (null unless `prepare` records a declared Marco-approved tag-set revision)
- `editor_agent`
- `editor_family`
- `change_level` (null except for `change`)
- `change_reason` (null except for `change`)
- `failed_verification_passes` (initialized to zero; consecutive since the latest Human unblock)
- `baseline_verification_line` (required only for `small`)
- `required_verifier_family` (null for `planning` and `small`)
- `verifier_agent` (null until approved, or always null for `planning` and `small`)
- `verifier_family`
- `status`
- `write_attempt_id` (unique for the current notes mutation attempt; invalidated by recovery)
- `in_flight_at`
- `in_flight_hostname`
- `in_flight_pid`
- `in_flight_process_start` (disambiguates PID reuse)
- `created_at` (set at `start`, when the row and its lock are first created)
- `approved_at`
- `completed_at`
- `research_queue_moved_at`
- `notes_written_at`
- `destination_moved_at`

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

Every `dish` command execution logs an event regardless of outcome:

- command name, timestamp, attributed agent, task GID (when applicable), submission ID (once one
  exists); `submit` uses the submission's recorded editor because it accepts no new attribution;
- full outcome: pass/fail, and on failure, every specific rule that failed — not just "validation
  failed" — so Marco can see which rules trip in practice and which never fire;
- for `start`: submission kind, frozen protocol release/Git binding, and any declared change level
  and reason;
- for `prepare`: which manifest was used, whether the note passed validation, the normalized
  exemption tags, and any declared exemption revision;
- for `approve`/`reject`: verifier agent/family, the decision, and for `reject`, the stated reason,
  so rejection patterns are visible without reading every case individually;
- for `submit`: the final submission state (`consumed`, `written` after a move failure, reverted to
  `ready` on confirmed non-application, or `uncertain`), so every outcome is visible in the log, not
  just returned to the caller.

`inspect` logs the attributed reader and returned state but not a duplicate copy of the frozen
bundle. Every logged outcome uses the same stable code returned in the CLI JSON envelope.

The generic Asana CLI's managed-task check also logs during v1a even though it does not yet block:
every note-write to a section-managed task made *outside* the guarded `dish` path is logged as an
advisory bypass event (task GID, command used, agent if known). This is the direct evidence for the
v1a-to-v1b decision — whether it's safe to flip the block on depends on how much real, legitimate
traffic would have been blocked, not a guess.

A periodic summary — a query over `audit_events`, not a new mechanism — should be able to answer at
minimum:

- how many `create`/`read`/`inspect`/`start`/`prepare`/`approve`/`reject`/`submit` calls happened, by
  agent, submission kind, and change level where applicable;
- validation failure rate, broken down by which specific rule failed most often;
- rejection rate, repeated-rejection-on-same-task rate, and Human-escalation/unblock rate;
- how many advisory bypass events occurred outside the guarded path, and on which tasks/agents.

Further queries (small-change diff characterization, write/reset frequency) are v2 candidates, tied
to mechanisms not built in v1a either — see `dish-tool-future.md`.

## Integration with the existing Asana CLI

The dish tool and general Asana CLI may share SDK client construction, task reads, task updates, and
error formatting. They must not share unguarded note-writing behaviour.

The general CLI consults the dish tool's live managed-task determination before changing notes —
advisory/logged in v1a, blocking in v1b (see `dish-tool-future.md`, Versioning plan;
Protocol-managed task registry, above).

The dish tool performs its final update through a separate guarded gateway that cannot be called
without a valid, `ready` submission.

## ChatGPT workflow

ChatGPT has no local CLI or SQLite access, so it cannot run any `dish` command itself.

Before ChatGPT authors anything, a local agent or Marco runs `dish start --agent gpt` with the
appropriate submission kind, then supplies ChatGPT the returned frozen release and exact protocol
documents. An already-authored file cannot be retroactively bound to a release; it must be reviewed
and regenerated under the frozen bundle.

ChatGPT's output is one complete candidate-note file. Except for a planning-only submission, that
file must already include ChatGPT's own `Self-verified: gpt, <date>` line, attested by ChatGPT as
part of producing the note — the same self-review requirement every other editor meets by writing
the line itself. A local agent or Marco does not add or backfill this line on ChatGPT's behalf: if
it's missing, `dish prepare` fails exactly as it would for any other editor's missing
`Self-verified:` line, and the fix is a corrected file from ChatGPT, not a local insertion.

A local agent or Marco then continues, declaring `--agent gpt` on commands that accept attribution —
attributing the submission to ChatGPT as editor even though a local process runs the commands on its
behalf:

1. runs `dish prepare` with ChatGPT's file;
1. for `initial` or `large`, arranges `dish approve`/`reject` from the opposite (Claude) family per
   the standard routing rule — nothing ChatGPT-specific, since GPT and Codex are one family;
1. runs `dish submit` once ready.

ChatGPT cannot declare its own `--agent` value or run any command itself — a human or local agent
does so on its behalf, honestly reflecting who actually authored and reviewed the note.

## Testing requirements (v1a)

Implementation follows TDD. Tests must cover:

- SQLite schema and migrations;
- tool-owned bare task creation in Research Queue, including clear versus ambiguous failure;
- tool-owned complete reads, including read-only access to excluded Cooking sections;
- submission inspection in every state, returning the frozen bundle, routing, completion markers,
  and exact legal next agent actions without claiming to store the controlled-handoff candidate;
- the common JSON result envelope, stable outcome/rule codes, retryability, allowed-action mapping,
  and exit status for every success and failure class;
- generic note-write advisory logging in v1a, and blocking once v1b is enabled, including
  `create_task` with notes for a managed or unresolved destination while bare creation remains
  allowed;
- non-note generic writes remaining allowed in both v1a and v1b;
- declared agent-name validation and agent-family routing;
- planning, initial, and change submission kinds; change-level arguments required only for change;
- kind-specific start eligibility: empty notes for planning, a valid Planning brief for initial, and
  a valid complete task for change, always inside the managed Cooking scope;
- planning receives its literal manifest and exemption-tag checks and advances directly to `ready`,
  with no `Self-verified:` or verifier requirement;
- initial routes to whole-task opposite-family verification without a Material changes entry;
- small and large change-level handling and their Process Record and verifier-routing consequences;
- the release resolver loads the complete checked-in protocol set, fails closed on missing,
  ambiguous, incomplete, or dirty sets, and chooses the correct role-specific manifest;
- release and manifest binding occurs at `start`, is returned for authorship, and remains frozen even
  if the current release changes before `prepare` or `submit`;
- a `small` submission preserves the baseline `Verification:` line byte-for-byte while recording the
  current governing release in submission/audit state, and fails rather than silently migrating an
  old incompatible structure;
- verifier-family mismatch on `approve`;
- every literal template-shape rule individually, including missing, duplicate, and unknown
  headings/labels, without interpreting their values;
- `Exemptions:` required in planning and complete-task candidates; `None` and each allowed nutrition
  tag accepted; unknown, duplicate, mixed-`None`, and explanation-less values rejected;
- `Destination section` required to resolve to the named non-queue Cooking section name/GID pair and
  frozen for approval and the submission's final conditional move;
- initial and change submissions preserve the normalized live Planning exemption set; changed sets
  require a recorded `--exemption-revision`, while small changes reject any exemption change;
- `--exemption-revision` rejected for planning, small, and unchanged tag sets;
- `approve` rejects a final file whose exemption set differs from the prepared set, requiring a
  return to `drafting` and a new `prepare` for any Human-approved revision;
- `approve` reparses Destination and returns a name/GID mismatch to `drafting` for a new `prepare`
  without incrementing the failed-verification counter;
- verifier-authored clear corrections accepted by `approve`, with deterministic validation rerun on
  the complete corrected file;
- `dish reject` returning the submission to `drafting` while retaining the lock, followed by a
  corrected `prepare` on that same submission;
- a second rejection transitioning to `awaiting_human`; all agent workflow commands remaining
  blocked until `dish-admin unblock` records a concrete change and resets the consecutive counter;
  the second rejection requiring `--changed-since-prior` and producing the complete Human Review
  escalation summary;
- verifier `--take-ownership` updating the editor and flipping the family required after the next
  successful `prepare`, while `approve --correction small` retains same-pass signoff;
- `dish reject` rejecting a call from an agent whose family does not match
  `required_verifier_family`, exactly as `approve` does;
- concurrent `dish start` on a task with an already-open submission (including one still in
  `drafting`) rejected, both by application check and by the SQLite unique constraint — the lock;
- `dish prepare`/`approve`/`reject`/`submit` called against a nonexistent or wrong-status
  `submission-id` rejected;
- `dish prepare --agent` differing from the recorded editor rejected unless ownership was transferred
  through the material-correction path;
- no Asana mutation on any pre-write failure; exactly one Asana mutation attempt per `submit` call
  that reaches the API;
- submission reuse rejection after `consumed`;
- two simultaneous `submit` calls on one submission;
- a submission is single-use: a second `submit` call against an already-`consumed` submission is
  rejected;
- confirmed non-application preserving retry eligibility (`in_flight` → `ready`);
- write-attempt identity and compare-and-swap protection preventing a stale process from recording a
  result for a recovered or replacement attempt;
- pre-send failures and explicit backend rejections classified as confirmed non-application, while
  possibly-sent timeouts/resets, HTTP 5xx, response-decoding failures, cancellation, and unknown SDK
  phases classify as `uncertain`;
- a confirmed notes write followed by a failed destination move preserving `written` and retrying
  only the move, never the notes write;
- an uncertain `submit` outcome is logged as `uncertain` and left for Marco to resolve via
  `dish-admin recover` to `ready` or `written`, without asserting any automatic outcome-table
  behaviour;
- `dish-admin recover` refuses a live recorded process or an unelapsed quarantine interval, requires
  an inspected outcome and reason, and invalidates the recovered attempt atomically;
- `dish-admin discard` releases only `drafting`, `research_handoff`, `awaiting_verification`,
  `awaiting_human`, `ready`, or `written` submissions,
  logs its reason, never mutates Asana, and rejects `in_flight`, `uncertain`, or terminal states;
- raw `notes`/`html_notes` bypass attempts;
- a missing `Self-verified:` label fails `prepare`; V1 does not parse its value;
- a ChatGPT-authored file missing the `Self-verified:` label fails `prepare`, and no local-agent
  insertion satisfies the authorship requirement even though V1 cannot verify that judgment;
- `editor_agent == verifier_agent` on `approve` is unreachable — always rejected by the family check
  first, confirming no separate collision path exists to test;
- the frozen `canonical_manifest` captured at `start` remains authoritative through `submit`;
- section-GID resolution: a `Sourcing`/`Reference` rename does not change managed status; an
  unresolvable section fails closed to managed;
- Research Queue → Verification Queue at accepted Research and Verification Queue → validated
  Destination after the notes write, including idempotent retries and manual-position overrides;
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
- governing non-note task fields other than tool-owned creation and conditional queue moves;
- automatically migrating every existing dish task's content to the current canonical structure;
- modifying the protocol text or incident logs;
- providing a remote or multi-user trust service;
- documenting the dish admin tool's location or invocation for agents;
- providing a dedicated revoke-management command (Marco uses the Asana web UI directly instead);
- a speed bump against an honest agent carelessly mis-declaring a material change as `small`
  (Marco's standing concern) — tracked in `dish-tool-future.md` for v2, once v1a's logging of what
  real `small`-declared diffs touch gives the input needed to design it.
