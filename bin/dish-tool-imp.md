# Dish tool — v1a implementation plan

Scope: build and soft-launch the complete v1a guarded workflow specified by `dish-tool.md`. V1a
performs real backend writes but leaves the generic Asana CLI's managed-task guard advisory-only;
v1b later flips that existing guard to blocking.

The tool-independent three-way protocols ship first. Tool development can proceed against fixtures,
but live rollout waits for the later tool-aware beta of `dish-planning-protocol.md`,
`dish-research-protocol.md`, and `dish-verification-protocol.md`. That beta supplies the final
agent-facing command workflow and governed machine-readable manifest. Do not add tool instructions
to the current tool-independent beta.

This plan implements decisions already settled in `dish-tool.md` and
`~/honest-pantry/dish-docs-design.md`; it does not reopen them. In particular:

- the tool owns governed task creation, reads, complete note writes, and both queue moves;
- Asana is a backend detail, not part of the tool-aware agent workflow;
- the second unsuccessful verification pass escalates to Human Review in v1a;
- every multi-step operation resumes only missing work;
- exact-content binding, live-baseline comparison, and external-edit detection remain deferred.

## Rollout

Land Steps 0–9 as independently testable commits. Steps 1–8 may use a committed fixture release
while the tool-aware protocol beta is being prepared. Do not point the live tool at Cooking tasks
until Step 0's final release assets exist and the tool-aware beta has passed its separate validation.

## Step 0 — tool-aware protocol release assets

The later beta, not the current tool-independent protocols, must provide one governed release set:

- the three role-specific protocol files;
- one machine-readable manifest/schema set for the Planning brief and complete task;
- one wrapper-owned human-readable `protocol_release` version file.

The manifest is the sole structural source for required, optional, exact-once, and allowed headings
and labels. It also defines the narrow parseable grammar for `Exemptions:` and `Destination section`.
The release wrapper advances the version atomically whenever any governed file changes and rejects
dirty, incomplete, ambiguous, or unversioned sets.

Before the real assets land, use fixture copies with the same filenames and manifest version. The
implementation must not fall back to `dish-protocol.md` or invent a compatibility mode.

## Step 1 — shared library, schema, and release resolver

Create `bin/dish_lib.py`, shared by the separate `dish` and `dish-admin` executables:

- a small Asana client/auth/error layer contained in `dish_lib.py` for v1a;
- SQLite setup and migrations for `~/ai-tools/var/dish-tool.db`, with `var/` gitignored;
- release resolution from the honest-pantry Git worktree;
- manifest parsing and literal note validation;
- agent-family routing (`claude` versus the `gpt`/`codex` family);
- managed-section and queue/destination helpers using immutable section GIDs;
- one audit helper used on every success and failure path.

The resolver loads the complete committed release, verifies its wrapper-owned version binding, and
fails closed on a missing, dirty, ambiguous, incomplete, or malformed set. `dish start` stores the
exact role-specific protocol bundle and manifest so the release remains frozen through signoff.

Implement the `submissions` and `audit_events` models from `dish-tool.md`, including:

- the partial unique index allowing one non-terminal submission per task;
- `failed_verification_passes`;
- the validated `destination_section_gid`;
- `research_queue_moved_at`, `notes_written_at`, and `destination_moved_at` completion markers;
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
- audit rows support nullable submission IDs while retaining task GIDs whenever known.

## Step 2 — `dish create`, `dish read`, and `dish start`

Create `bin/dish` with argparse subcommands and trusted `--agent claude|gpt|codex` attribution.

`dish create` creates one bare task in Cooking's Research Queue and returns its GID. It never writes
notes. Clear API rejection fails; an ambiguous create outcome is logged and reported for Marco to
resolve, without an automatic retry that could duplicate the task.

`dish read` returns the complete live task through the backend abstraction. It permits reads of
excluded Cooking sections because it makes no mutation.

`dish start`:

- confirms the task is a protocol-managed Cooking task;
- validates kind-specific starting shape: empty notes for `planning`, the Planning manifest for
  `initial`, and the complete-task manifest for `change`;
- validates change-level/reason arguments only and always for `change`;
- rejects an existing non-terminal submission by application check and database constraint;
- freezes and prints the release and exact documents the author must read;
- captures the Planning exemption set for `initial`/`change` and the exact Verification line for
  `small`;
- creates the `drafting` submission and returns its ID.

### Step 2 tests

- create uses the correct project/queue and produces no notes mutation;
- create distinguishes clear and ambiguous failure and never auto-retries the latter;
- read returns complete current content without applying management restrictions;
- each submission kind accepts only its valid starting shape;
- invalid project, excluded section, agent, kind, and change arguments fail before row creation;
- simultaneous starts produce exactly one open submission;
- every command invocation produces one audit event.

## Step 3 — `dish prepare` and Research handoff

`prepare` requires `drafting` for validation or `research_handoff` for a move-only retry. During
validation, `--agent` must equal the recorded editor. It validates the complete candidate against
the frozen role-specific manifest, preserving the exact baseline Verification line for `small`
changes.

Narrow value parsing is limited to:

- `Exemptions:` syntax, normalized-set preservation, and the approved revision flag rules;
- `Destination section`, which must resolve live to the named non-queue Cooking section GID.

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
- Destination section rejects queue, foreign-project, mismatched-name/GID, and missing sections;
- `prepare --agent` must equal `editor_agent`;
- status and verifier routing are correct for all kinds/levels;
- Research handoff handles source queue, already-moved, and manual-override cases;
- a move failure and retry never rerun an already-recorded transition.

## Step 4 — verification, correction routing, and two-pass escalation

`approve` requires `awaiting_verification` and the required opposite family. It accepts
`--correction none|small`, reruns deterministic validation and exemption equality over the complete
final file, records verifier attribution, and sets `ready`. It never moves the task onward. A
material verifier correction cannot be approved in place.

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
- `reject --take-ownership` changes editor attribution and the next verifier family;
- first rejection returns to drafting; second enters `awaiting_human`;
- second rejection requires `--changed-since-prior` and retains both rejection reasons in its
  escalation summary;
- prepare/approve/reject are blocked in `awaiting_human`;
- unblock requires the exact state and a non-empty concrete-change reason, resets the counter, and
  retains audit history;
- concurrent approve/reject calls permit only one transition.

## Step 5 — `dish submit`, destination handoff, and failures

`submit` accepts `ready` or `written`. From `ready`, it conditionally enters `in_flight` and makes
one complete notes update. It continues to trust the supplied controlled-handoff file: v1a does not
hash it or compare it with a saved live baseline.

Classify outcomes conservatively:

- confirmed non-application returns `in_flight` to `ready`;
- timeout, lost response, connection break, or uncertain server outcome becomes `uncertain`;
- confirmed success becomes `written` and records `notes_written_at`.

From `written`, never call the notes API. If the task is in Verification Queue, move it to the
validated Destination section; already-at-destination succeeds; a manual position outside both
queues is preserved; a planning submission remains in Research Queue. A move failure remains
`written` for a move-only retry. Once no move remains, set `consumed` and release the lock.

### Step 5 tests

- no pre-write failure reaches Asana and one submit invocation makes one SDK mutation call;
- simultaneous submits allow only one `ready` to `in_flight` transition;
- confirmed, uncertain, and successful write outcomes reach the specified states;
- retry from `written` never calls the notes API;
- destination, already-moved, planning, and manual-override cases behave correctly;
- a move failure resumes only the move and then consumes the submission;
- a consumed submission cannot be reused.

## Step 6 — remaining `dish-admin` recovery

Keep `dish-admin` a separate Marco-only executable.

- `recover` accepts only `in_flight` or `uncertain`, setting `ready` when Marco confirms notes were
  not applied or `written` when they were. Retrying `submit` from `written` completes only the move.
- `discard` accepts `drafting`, `research_handoff`, `awaiting_verification`, `awaiting_human`,
  `ready`, or `written`, records its reason, releases the lock, and never mutates Asana. It rejects
  `in_flight`, `uncertain`, and terminal states.

Tests cover every accepted/rejected source state, retained audit attribution, absence of backend
mutation, and the separation between `dish` and `dish-admin` command surfaces.

## Step 7 — generic CLI advisory integration

Before every generic note mutation, `bin/asana` consults `dish_lib.is_managed`. In v1a a managed or
unresolved target produces an advisory bypass event and the write proceeds. Cover `set-notes`,
`append`, `replace`, note-bearing batch operations, raw `notes`/`html_notes`, and note-bearing task
creation. Bare creation and non-note operations remain allowed. Section identity uses pinned GIDs,
not mutable names.

Tests cover all mutation surfaces, excluded sections, renamed sections, unresolved membership,
bare creation, and non-note writes. V1b changes only the advisory outcome to a block.

## Step 8 — reporting

Ship `bin/dish-reports.sql` with tested queries for command counts by actor/kind/level, validation
failure rates by rule, rejection and repeated-rejection rates, Human escalation/unblock rates,
submit outcomes, and advisory bypasses by task/agent.

## Step 9 — documentation and activation

- Update the later tool-aware protocol beta with only the agent-facing `dish` workflow; do not
  expose generic Asana or `dish-admin` instructions to agents.
- Add the short ChatGPT relay/runbook pointer required by `dish-tool.md` after the tool is usable.
- Run the protocol/tool integration suite against the exact release bundle.
- Validate the tool-aware beta separately, migrate the corpus using the snapshot-safe procedure in
  `dish-docs-design.md`, and deliberately activate it. Do not leave mixed production authority.

## Out of scope

V1b enforcement, exact-content binding or hashes, live-baseline/external-edit detection, the
small-change carelessness speed bump, dependency surfacing, general field-value grammar, semantic
culinary checks, automatic uncertain-outcome recovery, and later scripted migrations remain outside
this plan.
