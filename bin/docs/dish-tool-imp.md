# Dish tool — V1 implementation plan

**Superseded for the combined rollout.** `dish-tool-update-imp.md` carries the current staged build
plan, revised for protocol compatibility. This document predates that revision; where the two
disagree, the update plan wins. It is kept for the design detail the update plan does not restate.

Scope: build and development-test the complete V1 guarded workflow specified by `dish-tool.md`.

The protocols and the tool ship together as one combined rollout, so this plan's steps land against
the rollout branch rather than waiting for a later tool-aware beta.

This plan implements decisions settled in `dish-tool.md`,
`~/honest-pantry/dish-docs-design.md`, and Marco's later decision that V1 owns structured titles;
it does not reopen them. In particular:

- the tool owns governed task creation, reads, complete title-and-notes writes, and both queue moves;
- Asana is a backend detail, not part of the tool-aware agent workflow;
- two unsuccessful verification passes set `Status: pending-human-review`, cleared only by Marco;
- every multi-step operation resumes only missing work;
- exact-content binding and drift detection are part of V1, and are what catches an edit made
  outside the guarded path; the generic Asana CLI is not modified to police writes.

## Implementation sequence

Land Steps 0–9 as independently testable commits. Steps 1–8 may use a committed fixture release
while the tool-aware protocol beta is being prepared. Do not point the live tool at Cooking tasks
until Step 0's final release assets exist and the tool-aware beta has passed its separate validation.

## Step 0 — tool-aware protocol release assets

The later beta, not the current tool-independent protocols, must provide one governed release set:

- the three role-specific protocol files;
- one machine-readable manifest/schema set for the Planning brief and complete task;
- one wrapper-owned human-readable `protocol_release` version file.

The manifest is the sole structural source for required, optional, exact-once, and allowed headings
and labels. It also defines the narrow parseable grammar for `Exemptions:`, `Destination section`,
and complete-task titles. The title schema defines the canonical role-tag set, bracket-marker
grammar, dish-name and recognition-phrase boundary, and deterministic rendering order.
The release wrapper advances the version atomically whenever any governed file changes and rejects
dirty, incomplete, ambiguous, or unversioned sets.

Before the real assets land, use fixture copies with the same filenames and manifest version. The
implementation must not fall back to `dish-protocol.md` or invent a compatibility mode.

## Step 1 — shared library, schema, and release resolver

Create `bin/dish_lib.py`, shared by the separate `dish` and `dish-admin` executables:

- a small Asana client/auth/error layer contained in `dish_lib.py`;
- SQLite setup and migrations for `~/ai-tools/var/dish-tool.db`, with `var/` gitignored;
- release resolution from the honest-pantry Git worktree;
- manifest parsing and literal title/note validation;
- agent-family routing (`claude` versus the `gpt`/`codex` family);
- managed-section and queue/destination helpers using immutable section GIDs;
- one JSON result-envelope/error helper shared by every command;
- conservative request-phase and process-identity tracking for combined title-and-notes writes;
- one audit helper used on every success and failure path.

The resolver loads the complete committed release, verifies its wrapper-owned version binding, and
fails closed on a missing, dirty, ambiguous, incomplete, or malformed set. `dish start` stores the
exact role-specific protocol bundle and manifest so the release remains frozen through signoff.

Implement the `submissions` and `audit_events` models from `dish-tool.md`, including:

- the partial unique index allowing one non-terminal submission per task;
- `failed_verification_passes`;
- `baseline_title` and parsed `baseline_title_fields`;
- `prepared_title` and parsed `prepared_title_fields` accepted at the latest successful `prepare`
  or verifier small correction;
- the validated `destination_section_name` and `destination_section_gid`;
- write-attempt ID, `in_flight_at`, hostname, PID, and process-start identity;
- `research_queue_moved_at`, `task_content_written_at`, and `destination_moved_at` completion
  markers;
- every specified lifecycle state, including `research_handoff`, `awaiting_human`, and `written`.

Transactions and conditional updates must make competing state transitions fail explicitly. Do not
add candidate hashes, a live-notes baseline, a stale state, or external-edit detection.

### Step 1 tests

- schema creation and migration are idempotent;
- the partial unique index covers every non-terminal state and releases on terminal states;
- the resolver accepts one complete clean fixture release and rejects every incomplete, dirty,
  ambiguous, malformed, or version-mismatched variant;
- the exact frozen bundle and manifest survive later changes to the current fixture release;
- agent mapping, section resolution, and fail-closed management behave as designed;
- title parsing and rendering round-trip canonical values and reject malformed or ambiguous titles;
- result codes, retryability, allowed actions, and exit statuses follow the common JSON contract;
- audit rows support nullable submission IDs while retaining task GIDs whenever known.

## Step 2 — `dish create`, `dish read`, `dish inspect`, and `dish start`

Create `bin/dish` with argparse subcommands and trusted `--agent claude|gpt|codex` attribution.
Override argparse's default prose failures so argument and startup errors also use the common JSON
envelope.

`dish create` creates one bare task with a free working title in Cooking's Research Queue and returns
its GID. It never writes notes. Research later replaces that working title with the canonical
structured title. Clear API rejection fails; an ambiguous create outcome is logged and reported for
Marco to resolve, without an automatic retry that could duplicate the task.

`dish read` returns the complete live task through the backend abstraction, including the raw title
and its parsed structured fields when canonical. It permits reads of excluded Cooking sections
because it makes no mutation.

`dish inspect` returns, for every submission state, its kind, state, attribution, required verifier
family, frozen protocol/manifest bundle, baseline and prepared title fields, destination name/GID,
completion markers, and legal next agent actions. Candidate note content remains the controlled file
handoff and is not stored or returned by v1; structured title state is stored because `submit` owns
its final rendering and write.

`dish start`:

- confirms the task is a protocol-managed Cooking task;
- validates kind-specific starting shape: empty notes for `planning`, the Planning manifest for
  `initial`, and the complete-task manifest for `change`;
- captures the raw live title for every kind; `change` additionally requires and stores a canonical
  parse, while planning and initial construction may begin from a free working title;
- validates change-level/reason arguments only and always for `change`;
- rejects an existing non-terminal submission by application check and database constraint;
- freezes and returns the release and exact documents the author must read in the JSON `data` field;
- captures the Planning exemption set for `initial`/`change` and the exact Verification line for
  `small`;
- creates the `drafting` submission and returns its ID.

### Step 2 tests

- create uses the correct project/queue and produces no notes mutation;
- create distinguishes clear and ambiguous failure and never auto-retries the latter;
- read returns complete current content and parsed canonical title fields without applying
  management restrictions;
- inspect returns the frozen handoff instructions, routing, state, markers, and exact allowed-action
  mapping in both active and terminal states;
- each submission kind accepts only its valid starting shape;
- change start rejects a noncanonical live title, while planning and initial accept a working title;
- invalid project, excluded section, agent, kind, and change arguments fail before row creation;
- simultaneous starts produce exactly one open submission;
- every command invocation produces one audit event.

## Step 3 — `dish prepare` and Research handoff

`prepare` requires `drafting` for validation or `research_handoff` for a move-only retry. During
validation, `--agent` must equal the recorded editor. It validates the complete candidate against
the frozen role-specific manifest, preserving the exact baseline Verification line for `small`
changes.

For `initial` and `change`, `prepare` also requires one complete structured title declaration:

- `--dish-name` and `--recognition` are non-empty and may not contain title-control brackets;
- exactly one of one-or-more repeatable
  `--role side|dessert|component|condiment|benchmark|comparison` or `--no-role-tags` is required;
- exactly one of one-or-more repeatable non-empty `--blocker` or `--no-blockers` is required; and
- known role tags render first in manifest order, followed by blocker markers in declared order,
  then `<dish name> — <recognition phrase>`.

Planning stores its unchanged free working title as the prepared title. The tool guarantees
complete-task title grammar, explicit declaration, and deterministic rendering; Research and
Verification remain responsible for whether roles and blockers are complete, truthful, and
coherent with readiness and the notes.

Narrow value parsing is limited to:

- `Exemptions:` syntax, normalized-set preservation, and the approved revision flag rules;
- `Destination section`, whose name/GID pair must resolve live to the named non-queue Cooking
  section and be stored together.

All other values remain verifier/editor responsibility. Report every structural failure in one
attempt and leave the submission in `drafting`.

On success, planning and `small` become `ready`. Initial and `large` set the opposite
`required_verifier_family`, enter `research_handoff`, and conditionally move Research Queue to
Verification Queue. Already-in-Verification is success; a task outside both queues is a preserved
manual override. After recording completion, advance to `awaiting_verification`. A retry from
`research_handoff` performs only the missing move.

### Step 3 tests

- every required/exact-once/unknown heading and label rule fails independently;
- Planning and complete-task manifests select the correct rules;
- exemption syntax, preservation, and revision cases match the design matrix;
- Destination section rejects queue, foreign-project, mismatched-name/GID, and missing sections and
  stores the accepted pair;
- complete-task prepare requires exactly one side of each role/blocker declaration pair, stores the
  structured fields, and renders the canonical title deterministically;
- malformed, duplicate, reserved, bracket-containing, or ambiguous title inputs fail together with
  other deterministic validation errors;
- `prepare --agent` must equal `editor_agent`;
- status and verifier routing are correct for all kinds/levels;
- Research handoff handles source queue, already-moved, and manual-override cases;
- a move failure and retry never rerun an already-recorded transition.

## Step 4 — verification, correction routing, and two-pass escalation

`approve` requires `awaiting_verification` and the required opposite family. It accepts
`--correction none|small`, reruns deterministic validation, exemption equality, and live Destination
resolution over the complete final file, records verifier attribution, and sets `ready`. The
Destination name/GID must equal the pair accepted at `prepare`; a mismatch returns to `drafting` for
a fresh `prepare` without incrementing the failed-pass counter. Approval never moves the task
onward. A material verifier correction cannot be approved in place.

Approval uses the prepared structured title by default. With `--correction small`, the verifier may
supply one complete replacement title declaration; the tool reruns title validation and atomically
replaces the stored prepared fields. Partial title patches are never accepted. A material title
correction follows rejection and a fresh `prepare`, like any other material correction.

`reject` uses the same verifier-family check and records a complete reason. `--take-ownership`
records the verifier as the new material editor, causing the next successful `prepare` to route to
the opposite family; without it, editor attribution stays unchanged.

The first rejection since start or the latest Human unblock increments
`failed_verification_passes` and returns to `drafting`. The second requires
`--changed-since-prior`, increments the counter, and transitions to `awaiting_human`. Its audit event
combines both rejection reasons and the stated change into the Human Review escalation summary. From
that state all agent workflow commands are blocked.

`dish-admin unblock <submission-id> --reason "<concrete change>"` is Marco-only. It requires
`awaiting_human`, records the changed evidence, premise, method, or scope, resets the consecutive
counter, and returns to `drafting` without erasing prior events or releasing the task lock.

### Step 4 tests

- family mismatch and wrong-state calls fail for approve and reject;
- `approve --correction small` accepts a structurally valid correction; material correction cannot
  be approved through that path;
- approval preserves the prepared title when no replacement is supplied and accepts only a complete
  valid replacement with a declared small correction;
- a changed or newly misresolved Destination returns to `drafting` without counting a verification
  rejection, while an unchanged pair proceeds;
- `reject --take-ownership` changes editor attribution and the next verifier family;
- first rejection returns to drafting; second enters `awaiting_human`;
- second rejection requires `--changed-since-prior` and retains both rejection reasons in its
  escalation summary;
- prepare/approve/reject are blocked in `awaiting_human`;
- unblock requires the exact state and a non-empty concrete-change reason, resets the counter, and
  retains audit history;
- concurrent approve/reject calls permit only one transition.

## Step 5 — `dish submit`, destination handoff, and failures

`submit` accepts `ready` or `written`. From `ready`, it creates a unique write-attempt ID and
conditionally enters `in_flight`, recording its timestamp, hostname, PID, and process-start token
before making one backend request that updates the stored prepared title and supplied complete
notes together. See `dish-tool-update-imp.md` for the exact-content baseline and drift check that
now apply here. Every later state update uses the attempt ID in its
conditional update so a stale process cannot commit an outcome after recovery.

Classify outcomes conservatively:

- a proven pre-send local failure or well-formed explicit backend rejection is confirmed
  non-application and returns `in_flight` to `ready`;
- timeout/reset/lost response after sending may have begun, HTTP 5xx, malformed or undecodable
  response, cancellation after sending may have begun, or unknown SDK send phase becomes
  `uncertain`;
- confirmed success becomes `written` and records `task_content_written_at`.

From `written`, never repeat the title-and-notes API call. If the task is in Verification Queue,
move it to the validated Destination section; already-at-destination succeeds; a manual position
outside both queues is preserved; a planning submission remains in Research Queue. A move failure
remains `written` for a move-only retry. Once no move remains, set `consumed` and release the lock.

### Step 5 tests

- no pre-write failure reaches Asana and one submit invocation makes one SDK mutation call;
- that one mutation contains both the exact stored prepared title and supplied complete notes;
- simultaneous submits allow only one `ready` to `in_flight` transition;
- write-attempt identity prevents stale completion after an administrative recovery;
- confirmed, uncertain, and successful write outcomes reach the specified states;
- the full request/transport/response exception matrix defaults to `uncertain` whenever the client
  cannot prove non-application;
- retry from `written` never repeats either title or notes;
- destination, already-moved, planning, and manual-override cases behave correctly;
- a move failure resumes only the move and then consumes the submission;
- a consumed submission cannot be reused.

## Step 6 — remaining `dish-admin` recovery

Keep `dish-admin` a separate Marco-only executable.

- `recover` accepts only `in_flight` or `uncertain`. It refuses while the recorded process identity
  is live or before a fixed quarantine interval exceeding the maximum request lifetime plus safety
  margin has elapsed. It requires `--outcome not-applied|applied` and a concrete inspection reason,
  invalidates the old attempt ID atomically, and sets `ready` or `written` accordingly. Retrying
  `submit` from `written` completes only the move; `applied` means the combined title-and-notes
  mutation applied, never one field independently, and requires Marco to inspect both live fields.
- `discard` accepts `drafting`, `research_handoff`, `awaiting_verification`, `awaiting_human`,
  `ready`, or `written`, records its reason, releases the lock, and never mutates Asana. It rejects
  `in_flight`, `uncertain`, and terminal states.

Tests cover every accepted/rejected source state, live/dead/PID-reuse process identities, quarantine
boundaries, attempt invalidation, retained audit attribution, absence of backend mutation, and the
separation between `dish` and `dish-admin` command surfaces.

## Step 7 — drift detection

`bin/asana` is not modified. A guard there would cover only the local CLI agents, which already
prompt Marco before any Asana write, and never ChatGPT, which writes through its own Asana
integration — cost without coverage.

Instead, the tool compares the live title and body against its recorded exact-content version on
every read. A mismatch is a drift event: it voids any signoff, drops the task out of guarded state,
and is logged with the task and the invalidated signoff. Section identity uses pinned GIDs, not
mutable names.

Non-body operations — `due_on`, completion, other fields — remain freely available outside the tool
and never trigger drift.

Tests cover detection on title and on body, excluded sections, renamed sections, unresolved
membership, tool-owned bare creation, and non-body writes not registering as drift.

## Step 8 — reporting

Ship `bin/dish-reports.sql` with tested queries for command counts (including `inspect`) by
actor/kind/level, validation failure rates by rule, rejection and repeated-rejection rates, Human
escalation/unblock rates, submit outcomes, and drift events by task.
Include title-validation failures and title-versus-note generic bypasses distinctly.

## Step 9 — documentation and release preparation

- Update the later tool-aware protocol beta with only the agent-facing `dish` workflow; do not
  expose generic Asana or `dish-admin` instructions to agents.
- Make the beta require structured title declarations for complete-task `prepare` and document that
  the tool, not the agent or relay runner, renders and writes the final title.
- Add the short ChatGPT relay pointer required by `dish-tool.md` after the tool is usable.
- Run the protocol/tool integration suite against the exact release bundle.
- Write and test the production activation runbook without executing it. Development and fixture
  validation must perform no live Cooking-task write.

## Rollout to production — separate future authorization required

Do not begin this section merely because Steps 0–9 pass. Live migration, activation, changes to
governing agent instructions, and any live Cooking-task write require Marco's separate execution
approval. Until then, `CLAUDE-global.md` must not mention or route agents to the development tool.
The repository `CLAUDE.md` remains design guidance only; production agent routing is added to the
global file during the authorized cutover.

Before cutover:

- prepare one exact release bundle containing the three validated tool-aware protocols, both
  canonical manifests, and the wrapper-owned `protocol_release` file;
- run the complete protocol/tool integration suite against those exact committed bytes without a
  live Cooking-task write;
- confirm the release resolver accepts the clean committed bundle;
- confirm the agent-facing protocols and short ChatGPT relay expose only the `dish` workflow, not
  generic Asana commands or `dish-admin`; and
- record the source snapshot, release identity, migration result, unresolved tasks, and rollback
  point required by the snapshot-safe migration procedure in `dish-docs-design.md`.

Then perform one deliberate cutover:

1. Hold protocol-managed title and note changes for the migration window.
2. Revalidate the exact release bundle and tool integration suite.
3. Migrate and verify the complete managed corpus using the snapshot-safe procedure. If any task
   cannot be migrated or explicitly dispositioned, stop with the old authority intact.
4. Switch the governing protocol release and any selected agent-routing instructions together.
5. Verify that all managed tasks and new work resolve to the same active release before reopening
   writes.

Do not leave mixed production authority. If cutover verification fails, restore the recorded
snapshot and previous governing release before reopening writes.

The initial migration establishes canonical title syntax but does not invent missing semantic
knowledge. Normalize known role and blocker markers mechanically. Where the snapshot cannot support
an honest blocker declaration without judgment, use the manifest-defined `[blockers unreviewed]`
marker and repair that task later through an ordinary guarded change. This keeps V1's architecture
stable without making comprehensive title repair a cutover dependency.

## Out of scope

V1b enforcement, exact-content binding or hashes, live-baseline/external-edit detection, the
small-change carelessness speed bump, dependency surfacing, general field-value grammar, semantic
culinary checks, automatic uncertain-outcome recovery, and later scripted migrations remain outside
this plan. Blocker filtering and systematic semantic title repair are v2 follow-ups. Change-diff
telemetry remains optional unless deliberately selected below.

## Consider adding

The remaining item is not implementation authorization. Diff telemetry is a bounded addition to the
existing audit path.

### Change-diff telemetry — recommended

For `change` submissions, compare the candidate accepted at `prepare` with the live task notes and
add a compact summary to that successful invocation's audit event: characters added and removed,
lines added and removed, and canonical headings containing changed lines. Do not warn, block,
reclassify the change, or persist the source text. V1 already assumes no external edits during a
submission, so this does not add a saved live baseline or imply external-edit detection.

If selected, add the calculation and audit tests to Step 3 and report distributions by declared
change level in Step 8. The resulting `small`-change evidence informs v2's carelessness speed bump;
it does not predetermine that trigger or its enforcement.
