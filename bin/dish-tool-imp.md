# Dish tool — v1a implementation plan

Scope: v1a only, per `dish-tool-future.md`'s Versioning plan — the full guarded path
(`start`/`prepare`/`approve`/`reject`/`submit`/`dish-admin recover`/`dish-admin discard`),
soft-launched with the generic Asana CLI's managed-task check running advisory/log-only. v1b's
enforcement flip and all v2 items are out of scope here; nothing in this plan builds toward them ahead
of need.

This plan assumes the design in `dish-tool.md` as final for v1a. Where that document
already resolved a question, this plan does not re-litigate it — it cites the resolution and moves to
what building it requires. Genuinely open implementation judgment calls are marked **OPEN** with
options and a recommendation; nothing else in this plan should be read as still undecided.

**Upstream dependency, not yet resolved — flagged, not designed here.** `dish-tool.md`'s
Protocol release resolver section says the current single-file `$PROTOCOL_MD_PATH` mechanism (this
plan's Step 0/1) "remains until the three-way split ships." That split — `dish-protocol.md` becoming
`dish-planning.md` + `dish-research.md` + `dish-verification.md` — is confirmed design-only, not
implemented, in `dish-docs-design.md` (lines 70–412), and the planning-brief schema itself is still
being actively drafted there (line 389: "Draft compact `dish-planning.md` from the settled schema").
This plan does **not** invent that schema or the post-split multi-file resolver: Steps 0–1 below
continue to target the single `dish-protocol.md` file, exactly as the design doc's interim allowance
permits, and the manifest loader will need a follow-on revision once the split actually ships. Do not
build the post-split resolver speculatively.

## Rollout

Ship as staged commits: Step 0 → Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 → Step 7 → Step
8 → Step 9. Each stage lands and is independently testable. Steps 1–6 can be built and tested against a
fixture copy of the protocol text before Step 0 is merged; nothing in Steps 1–6 depends on Step 0
having landed in the real `dish-protocol.md`, but the tool must not soft-launch against live tasks
(Step 7 going live) until Step 0 is merged for real, since `prepare` cannot validate against a manifest
that doesn't exist yet.

See Deployment for the post-build go-live steps.

## Step 0 — protocol-text prerequisites (draft here, approve separately)

`dish-tool.md`'s validator assumes several things in `dish-protocol.md` that are not
there yet (confirmed by reading the live file). This is drafted here for your sign-off per the change
plan's "Approval package required before production changes" — it is a protocol-text edit, which is
explicitly out of scope for the *tool* itself, but it's a hard precondition for the tool to work at
all, so the plan can't skip past it.

**Already resolved, no change needed:** `protocol <revision>` — the protocol text (line 193) already
defines this as "the latest Git commit that changed this file. Without Git access, use the SHA-256 of
the exact protocol text used plus the date and time it was read." The tool implements exactly this
(see Step 1, Protocol release capture); no protocol-text addition required.

**1. Machine-readable canonical-structure manifest.** Add a fenced block near `## Canonical task`
(dish-protocol.md:55) enumerating the allowed headings and which are required, sourced from the
existing prose there (line 57: "Use only sections that carry information, except `WHAT TO BUY`, which
is always present") and the heading list at lines 68–99:

```json
{
  "manifest_version": 1,
  "headings": [
    {"name": "WHAT TO BUY", "required": true},
    {"name": "CHECK BEFORE COOKING", "required": false},
    {"name": "QUANTITIES", "required": false},
    {"name": "HOW TO COOK IT", "required": false},
    {"name": "WHAT SUCCESS LOOKS LIKE", "required": false},
    {"name": "WATCH OUT FOR", "required": false},
    {"name": "STORAGE", "required": false},
    {"name": "PROCESS RECORD", "required": true}
  ],
  "process_record_subheadings": ["Decisions", "Research basis", "Material changes", "Post-cook actuals", "Open questions"]
}
```

`manifest_version` lets the validator detect a future incompatible manifest shape without guessing.

**Checked against live tasks, not just the prose.** Spot-checked several real tasks across sections
(`Sichuan`, `Subcontinent`, `Eating`) via `asana notes`. All top-level headings above matched exactly —
nothing extra, nothing missing. One gap surfaced and is now fixed: task `1216471568411594` (Lushui
master stock) has an `### Open questions` subheading under `PROCESS RECORD` that the original draft
didn't allow; it's added to `process_record_subheadings` above. Still worth a final look before
approving, since this was a sample, not every live task. `process_record_subheadings` should
additionally be scoped as valid only nested under `PROCESS RECORD`, not as a second top-level
allowlist.

**Manifest scope: heading allowlist only, decided.** The change plan's older wording asks for a
manifest of "headings, required fields, and allowed values," but the design doc's Deterministic
validation checks list (`dish-tool.md`, `dish prepare`) already settles this downstream: it treats
`Stage:`/`Human review:`/`Verification:`/`Self-verified:` as fixed field names the validator checks
directly for presence only, and describes only the *heading* allowlist as manifest-sourced. The change
plan doesn't need to be brought back in sync with this — see `CLAUDE.md`'s authority-flow note.
Heading-only, matching the JSON above.

**Manifest encoding: fenced JSON, decided.** The block is parsed by the Python validator
(`json.loads`, two lines, stdlib-only), not read structurally by an LLM, so agent-parsing difficulty
isn't a real concern either way. JSON fails loudly on a malformed edit (missing bracket/comma),
matching this design's fail-closed philosophy elsewhere (e.g. unresolved section-GID → managed by
default) — the deciding factor over YAML (pleasant to hand-edit but indentation-sensitive, so a
careless prose edit near the block could silently misparse rather than error) or a hand-parsed plain
markdown list (avoids new syntax but reintroduces the brittle-regex risk already rejected for the
allowlist).

**2. `Self-verified: <agent>, <date>` as a required process-record line.** Add it as a fourth line in
the `## PROCESS RECORD` block (dish-protocol.md:86–89), alongside `Stage:`, `Human review:`, and
`Verification:` — already present in the live file as read for this plan, so this item is **done**,
not drafted. The tool's validator checks only that the label/line exists, per `dish-tool.md`'s Agent
identity and verifier routing section ("V1 checks that the label exists, not its value") — it does not
check that the named agent matches `editor_agent`. Whether the name is honest is a trusted-procedure
question (see the ChatGPT workflow section), not a machine-enforceable one.

**3. Statement that protocol-managed note writes go through the guarded tool.** Still needed as a
drafted sentence near `## Canonical task` or `## Workflow`:

> Protocol-managed task writes go through `dish-tool.md`'s `dish` command; as of
> v1a this is logged, not yet enforced — a direct edit still succeeds but is recorded as an advisory
> bypass event.

**4. A "compact Planning brief" template and its own manifest — OPEN, not drafted here.**
`dish-tool.md`'s Submission kinds section defines a `planning` kind that "writes the compact Planning
brief into a bare task" and validates it against "the planning manifest" — but no such template exists
in `dish-protocol.md` yet, and it should not be invented here: `dish-docs-design.md` records this as
live, unsettled design work (the planned `dish-planning.md` upstream document, schema still being
drafted as of that file's line 389). Building `dish start --kind planning` and its validator (Steps 2–3
below) is blocked on that schema landing, either in `dish-protocol.md` directly (pre-split, matching
this plan's continued single-file target) or in `dish-planning.md` once the split ships. **Recommended
sequencing:** build and ship Steps 1–7 for `initial`/`change` first: they don't depend on this; treat
`planning` support as a trailing addition once the schema is settled, rather than blocking the whole
v1a rollout on it.

Items 1 and 3 are drafts against the actual protocol prose style, ready for you to approve as-is or
edit — not a placeholder for you to write from scratch. Item 2 needs no further action. Item 4 needs a
design decision upstream before this plan can specify it.

## Step 1 — foundation: shared modules, schema, manifest parsing

**`dish_lib.py`'s own Asana client, not an `asana_lib.py` extraction yet.** Give `dish`/
`dish-admin` their own small client/auth/error-handling helpers inside `dish_lib.py` (same
shape as `asana`'s `load_pat`/`client()`/`_error_detail`/`_call`, duplicated rather than shared for
now). Extracting a shared `asana_lib.py` out of the already-shipped `asana` CLI is a refactor of
production code the new feature doesn't need in order to work — deferring it keeps v1a build risk
contained to new files only. Revisit consolidation once v1a is stable and proven; `asana`'s existing
tests are unaffected either way.

**`~/ai-tools/bin/dish_lib.py` (new)** — everything `dish` and `dish-admin` share:

- SQLite connection helper, schema creation/migration, targeting `~/ai-tools/var/dish-tool.db`
  (create `~/ai-tools/var/` if absent; add `var/` to `.gitignore` alongside the existing
  `__pycache__/`/`.venv/` entries).
- `submissions` and `audit_events` tables exactly as specified in `dish-tool.md`'s
  SQLite model, including the partial unique index on `submissions(task_gid)` for non-terminal
  `status`.
- **No content hashing, no live-notes baseline.** `dish-tool.md`'s Scope and `dish submit` sections
  are explicit that V1 "does not hash candidate content, save a live-notes baseline, or detect
  external edits" and that `submit` "trusts the controlled handoff to supply the final reviewed file"
  rather than binding it by hash or comparing against a saved baseline. An earlier draft of this plan
  built exactly that machinery (`canonicalize_and_hash`, `baseline_notes_hash`, a `stale` submission
  state) and verified it against a live Asana round trip; none of it is built in v1a per the current
  design, so it is cut from this plan rather than carried forward as dead code to test. The lock
  claimed by `dish start` is the only protection against a concurrent controlled edit; V1 explicitly
  accepts the residual risk of an edit made outside the guarded path entirely (web UI, integration,
  generic CLI) while that lock is held.
- Protocol release capture: `protocol_release()` runs `git -C <honest-pantry path> log -1
  --format=%H -- dish-protocol.md`; on any git failure, falls back to `sha256(protocol_text) + read
  timestamp`, matching the protocol text's own fallback rule (dish-protocol.md:193–194) exactly. Pre-
  split (single file), `release_commit` is the same value as `protocol_release` — the design's
  `submissions` columns keep them separate only because the *post-split* resolver distinguishes a
  named release (spanning three files) from one file's commit; until that split ships, they carry
  identical values and this plan does not build any logic that treats them differently.
- Manifest loader: reads `dish-protocol.md` (path from `$PROTOCOL_MD_PATH`, default
  `~/honest-pantry/dish-protocol.md`, mirroring `asana`'s `$ASANA_ENV` pattern), extracts and
  `json.loads`s the fenced manifest block added in Step 0, and returns `(canonical_manifest,
  protocol_release, release_commit, protocol_bundle)` — `protocol_bundle` is the exact frozen protocol
  text itself (per `dish-tool.md`'s SQLite model: `submissions.protocol_bundle`, "the exact
  role-specific checked-in protocol contents frozen at `start`"), not a hash of it. A missing or
  malformed manifest block is a hard failure — no `prepare` can proceed — consistent with fail-closed
  elsewhere in this design.
- Agent family routing: `family(agent) -> "claude"|"gpt"`, per the `claude` / `gpt,codex` mapping.
- Change-level mapping: `small|medium|large -> Local|Delta|Reconstruction` for the process record.
- Audit logging: one `log_event(...)` function writing to `audit_events`, called by every command
  path in Steps 2–7, including failure paths with no submission row.

**`$PROTOCOL_MD_PATH` default: `~/honest-pantry/dish-protocol.md`, decided.** honest-pantry is a
sibling directory to `ai-tools`, not a git submodule of it, so the tool reads it as a plain filesystem
path with its own git repo underneath (for `protocol_release()`'s `git log`). Not actually a plan-level
recommendation — the design doc already states this exact default, overridable via env var
(`dish-tool.md`:191), matching `asana`'s `$ASANA_ENV` pattern.

### Tests (`tests/test_dish_lib.py`, `tests/test_dish_schema.py`)

- schema creation is idempotent (running migration twice doesn't error or duplicate);
- partial unique index rejects a second non-terminal `submissions` row for the same `task_gid`, and
  permits a new row once the prior one is terminal (`consumed`/`discarded`);
- `protocol_release()` returns the git commit hash when git succeeds, and the SHA-256+timestamp
  fallback shape when git is unavailable (mock the subprocess call for both branches);
- manifest loader parses a valid fixture block correctly and returns the exact frozen `protocol_bundle`
  text alongside it; rejects (does not silently accept) a missing block, a malformed-JSON block, and a
  block with an unrecognized `manifest_version`;
- `family()` maps `claude` → `claude`, `gpt`/`codex` → `gpt`, and rejects any other value;
- `log_event` writes a row with `submission_id` nullable and `task_gid` populated whenever known.

## Step 2 — `dish start`

`~/ai-tools/bin/dish` (new) — dispatch shell using `argparse` (subparsers for `start`/`prepare`/
`approve`/`reject`/`submit`, `choices=` for `--kind`/`--change-level`, and for `--agent` on every
subparser except `submit`, which takes no `--agent` — see the design doc's Agent identity and verifier
routing section). No convention forces this to match `asana`'s hand-rolled flag parsing, and `argparse`
gets free `--help`, error messages, and `choices` validation for no extra code.

`d_start(...)` implements Workflow §1: confirms the task exists; confirms the kind is valid
(`planning`/`initial`/`change`) and that `--change-level`/`--change-reason` are present only and always
for `change`; for `planning`, confirms the task is bare (per `dish-tool.md`, "a `planning` submission
requires a bare task" — **OPEN**: exact definition of "bare" — e.g. empty notes vs. notes matching some
minimal template — is deferred with the rest of `planning` support per Step 0 item 4); confirms no other
open submission already exists for this task — enforced by application check and by Step 1's partial
unique index on `submissions(task_gid)` for non-terminal `status` (including `drafting`), so a race
between two simultaneous `start` calls fails at the database layer, not only in application logic —
this is the lock; resolves the current checked-in `protocol_release`/`release_commit`, loads the exact
role-specific protocol bundle and manifest, and stores them frozen on the row — this bundle governs
authorship, self-review presence, validation, and verification for the submission's entire life, and
`start` prints the release and exact documents the author must read before drafting; for a `small`
change, captures the task's existing `Verification:` line verbatim as `baseline_verification_line` for
later exact-match comparison at `prepare`; creates one `submissions` row, status `drafting`, and prints
the `submission_id` back to the caller — this is the only token every later command
(`prepare`/`approve`/`reject`/`submit`) operates on, there is no separate token object.

The lock is held for as long as the submission stays in a non-terminal status, and releases
automatically when the submission reaches `consumed` or `discarded` — see Submission states.
Returning a note to `drafting` via `reject` does not release it.

### Tests (`tests/test_dish_start.py`)

- `start` on a task with no open submission succeeds and creates a `drafting` row;
- `start --kind planning` on a task with existing (non-bare) notes is rejected;
- `start --kind small change` captures the task's current `Verification:` line verbatim onto the row;
  `start` for `initial`/`medium`/`large` does not require or capture one;
- `start` on a task with an already-open (non-terminal, including `drafting`) submission is rejected;
- two simultaneous `start` calls on the same task: only one succeeds (assert on the SQLite unique-index
  rejection for the second, not just "doesn't crash" — the race case Step 1's partial unique index
  exists for);
- `start` on a task that no longer exists is rejected before any row is created;
- `start` prints the frozen protocol release and the exact documents to read, and stores the frozen
  `protocol_bundle`/`canonical_manifest` on the row;
- every `start` call (pass or fail) produces exactly one `audit_events` row.

## Step 3 — `dish prepare` and deterministic validation

`d_prepare(...)` implements Workflow §2: confirms the submission exists and is in status `drafting`;
reuses the protocol release, bundle, and manifest frozen at `start` — it never resolves the current
release again, even if the checked-in protocol changes before `prepare` runs; runs the deterministic
validation rule set against `<candidate-note>` (see below); on a validation failure, reports every
violated rule, the submission stays in `drafting` (it already exists, opened by `start`), and the
attempt is logged; on a pass, advances the row out of `drafting` — `planning` or `small` change →
status `ready`, no verifier required; `initial`, `medium`, or `large` → status `awaiting_verification`,
with `required_verifier_family` set to the family opposite `editor_family`.

**Deterministic validation is literal template shape only — the design doc's own list is short, and
this plan does not add rules beyond it.** For `planning`, it checks the Planning brief heading and
required/exact-once labels from the planning manifest (blocked on Step 0 item 4). For a complete task
(`initial`/`change`):

- headings and labels that the canonical manifest marks required are present (this covers `WHAT TO
  BUY`, `PROCESS RECORD`, and the fixed process-record labels `Stage:`/`Human review:`/`Verification:`/
  `Self-verified:` — presence only, none of their values are parsed);
- headings and labels that must occur once occur exactly once;
- when `## QUANTITIES` is present, a `Portions:` label is present under it;
- no heading exists outside the manifest's allowlist.

**One additional check, for `small` changes only:** the submitted note's `Verification:` line must
match `baseline_verification_line` (captured at `start`) byte-for-byte. If it doesn't, `prepare` fails —
this is the one place V1 does compare submitted content against something captured earlier, and it
exists specifically because a Local change is not supposed to touch that line at all.

**What this plan explicitly does not check**, because `dish-tool.md`'s validation list doesn't ask for
it and "V1 does not parse or interpret" field values beyond bare presence (Agent identity and verifier
routing section): a readiness-line/`CAN I COOK IT?` contradiction check; whether the declared
`--change-level` matches anything written in the process record; whether the process record's own
`protocol <revision>` text matches the freshly-read release (the frozen release is tracked in
submission/audit state for provenance, not compared against note text); and whether `Self-verified:`
names the actual `editor_agent`. An earlier draft of this plan built all four; they are cut here because
the current design doc doesn't call for them, not because they were tried and failed.

The canonical allowlist is parsed from the manifest carried in the protocol text itself, not
duplicated by hand in this tool — required as part of v1a's scope, not a later addition. A
hand-maintained hardcoded allowlist would recreate, inside the validator meant to eliminate this
exact failure mode, the same silent-drift risk the tool exists to remove from the protocol's own
prose rules.

Deferred to V2 once the mechanical layer is proven: field grammar and value parsing; unresolved
structural placeholder detection (e.g. leftover `[approx]` markers); any judgment of whether an
omitted optional section should have been present; and content quality.

The validator performs no Asana mutation and does not decide whether the recipe is culinarily
correct, whether research is adequate, or whether the declared change level is semantically honest.

### Tests (`tests/test_dish_prepare.py`, `tests/test_dish_validation.py`)

Mirrors `dish-tool.md`'s Testing requirements section directly:

- each deterministic-validation rule (required heading/label presence, exactly-once occurrence,
  `Portions:`-under-`QUANTITIES`, heading allowlist) fails and passes independently — one test per
  rule, not one mega-test;
- a note with `## QUANTITIES` present but no well-formed `Portions:` line under it fails; a note
  without `## QUANTITIES` at all is unaffected by this rule (heading omission stays the verifier's
  call, per the heading-allowlist rule above);
- a missing `Self-verified:` label fails `prepare`, folded into the generic required-label-presence
  rule; a present label with any value — including one that does not name `editor_agent` — passes,
  since V1 checks only that the label exists;
- a small-change submission whose `Verification:` line does not match `baseline_verification_line`
  byte-for-byte fails `prepare`; an exact match passes;
- `prepare` validates against the protocol release, bundle, and manifest frozen at `start`, never
  re-resolving the current release, even if the checked-in protocol changes before `prepare` runs;
- heading outside the manifest allowlist fails; a heading present in the allowlist but absent from the
  note (and not `WHAT TO BUY`) does not fail, since omission-judgment is explicitly the verifier's job,
  not the validator's;
- `prepare` on a submission not in status `drafting` (nonexistent, or already past `drafting`) is
  rejected;
- successful `prepare` produces the correct initial `status` for each of `planning`/`small`/
  `medium`/`large`, and the correct `required_verifier_family` for `initial`/`medium`/`large`;
- `planning` receives only its literal manifest checks and advances directly to `ready`, with no
  `Self-verified:` or verifier requirement (blocked on Step 0 item 4 landing before this test can run
  for real; write it against a fixture manifest in the meantime if the real one isn't ready);
- every `prepare` call (pass or fail) produces exactly one `audit_events` row.

## Step 4 — `dish approve` / `dish reject`

`d_approve(...)`: verifier-family check against `required_verifier_family`; the verifier may submit a
complete file that differs from the one `prepare` validated — a clear correction, per `dish-tool.md`'s
`dish approve`/`dish reject` section ("the verifier may make a clear correction, recheck the complete
file, and sign it") — so `approve` reruns the same deterministic validation rule set on the verifier's
final file rather than comparing it against anything captured earlier; there is no content-hash gate
and no path that rejects an edited file solely for having changed. On pass, an atomic conditional
update — `UPDATE submissions SET status = 'ready', ... WHERE submission_id = ? AND status =
'awaiting_verification'`, checking the row count affected — records `verifier_agent`/`verifier_family`,
sets `ready`. A zero-row update (status already moved by a concurrent call) is reported as a conflict,
not silently treated as success.

`d_reject(...)`: same family check as `approve`; same conditional-update pattern (`WHERE status =
'awaiting_verification'`), but returns the submission to `drafting` — **not** a terminal state — and
logs the reason. The lock remains held while the editor corrects the note and runs `prepare` again on
the same submission; no new `start` is required or allowed for this cycle.

### Tests (`tests/test_dish_approve_reject.py`)

- verifier-family mismatch rejected on both `approve` and `reject`;
- successful `approve` transitions `awaiting_verification` → `ready` and records verifier fields;
- `approve` accepts a verifier-submitted file that differs from the one `prepare` validated (a clear
  correction), rerunning deterministic validation on the complete corrected file rather than diffing
  it against the earlier one;
- `reject` returns the submission to `drafting`, retaining the lock; a subsequent `prepare` call on the
  *same* `submission-id` (not a new `start`) can pass and advance it again;
- concurrent `approve`/`reject` on the same submission: only the first conditional update succeeds
  (row count 1); the second sees zero rows affected and is reported as a conflict, not applied on top
  of the first;
- `editor_agent == verifier_agent` is unreachable on `approve` — confirm the family check rejects it
  before any equality comparison would run, i.e. no separate collision-detection code path exists to
  test (per `dish-tool-future.md`, Dropped).

## Step 5 — `dish submit` and failure handling

`d_submit(...)` implements Workflow §4: loads the submission, requires status `ready`; trusts the
controlled handoff to supply the final reviewed file directly — per `dish-tool.md`'s `dish submit`
section, V1 does not bind it by hash or compare the live task against a saved baseline, so there is no
content-hash recomputation and no live-read staleness check here; atomically flips `ready` →
`in_flight`; makes one notes update call; on clear success marks `consumed` — the lock releases. A
submission is single-use: a second `submit` call against a `consumed` submission is rejected outright,
with no write-count budget, `--final` confirmation step, or reset mechanism (no incident evidences a
need for one — see `dish-tool-future.md`).

Failure classification is by status code, not "any mapped `ApiException` means confirmed failure" — the
design doc's own Failure behaviour section requires the tool to *know* the write wasn't applied before
reverting to `ready`, which a 5xx doesn't establish:

- **Confirmed non-application → `ready`** (safe to retry as-is): `400`/`401`/`403`/`404` (request
  rejected before any mutation could occur), and `429` once the SDK's own retry/backoff is exhausted
  (rate-limited means the request was never accepted for processing).
- **Uncertain → `uncertain`, resolved only via `dish-admin recover`**: `5xx` after the SDK's own
  retries are exhausted (a 500/502/503/504 does not prove the mutation was never applied), timeouts, and
  connection breaks with no response at all.

  **Verified: the SDK's retry/backoff is real, not assumed.** `asana==5.2.5`'s `Configuration()` carries
  a `urllib3.util.retry.Retry` with `total=5`, `status_forcelist=[429, 500, 502, 503, 504]`,
  `backoff_factor=2`, `backoff_max=120`. `update_task` (what `submit` calls) is a `PUT`, which *is* in
  urllib3's default retryable-methods set, so this retry strategy does apply to the write call itself,
  not just reads — the classification above is sound.

  One consequence worth being deliberate about in Step 5's implementation: because urllib3 retries `PUT`
  transparently *underneath* the SDK call, a single `submit()` invocation that eventually times out or
  raises may already represent up to 5 real `PUT` attempts against Asana, not zero or one. A timeout the
  tool observes is not evidence the write was never sent — it's evidence the tool never got a confirming
  response, which is exactly why the design routes it to `uncertain` rather than `ready`. This doesn't
  break the design (an idempotent PUT of the same complete note content is safe to have been applied more
  than once), but "exactly one Asana mutation call on the success path" (test list below) should be read
  as exactly one `submit()` invocation reaching the API, not a guarantee of exactly one HTTP request on
  the wire — worth a one-line comment in the test itself so a future reader doesn't misread a passing test
  as proving single-request behavior.

### Tests (`tests/test_dish_submit.py`)

- `submit` on a submission not in status `ready` is rejected;
- exactly one Asana mutation call on the success path, none on any pre-write failure path;
- each of `400`/`401`/`403`/`404`/`429`-after-retries reverts `in_flight` → `ready`, and the same
  submission can be retried;
- each of `5xx`-after-retries, a simulated timeout, and a connection break moves to `uncertain`, not
  silently retried and not left `in_flight` forever, and not misclassified as confirmed-`ready`;
- two simultaneous `submit` calls on the same submission: only one can flip `ready` → `in_flight`
  (assert on the second call's rejection, not just "doesn't crash" — this is the same
  race shape as `start`'s open-submission check, but at a different table state);
- a `consumed` submission cannot be resubmitted, even byte-identical, with no write-count budget,
  `--final` confirmation step, or reset mechanism to test (see `dish-tool-future.md`).

## Step 6 — `dish-admin recover` and `dish-admin discard`

`~/ai-tools/bin/dish-admin` (new, separate executable — deliberately not a hidden subcommand of
`dish`, per the design's "distinct binary/subcommand namespace" requirement).

`recover <submission-id> --status ready|consumed` sets a stuck `in_flight` or `uncertain` submission's
status by hand, once Marco has checked the live task directly in Asana and confirmed what actually
happened — no automated outcome table; the mechanism to compute one is a v2 candidate with no
evidenced need yet (see `dish-tool-future.md`).

`discard <submission-id> --reason "<reason>"` marks an abandoned `drafting`, `awaiting_verification`,
or `ready` submission `discarded`, releases its lock, and logs the reason, without mutating or changing
the lifecycle state of the Asana task. It rejects `in_flight`, `uncertain`, and terminal submissions
and never runs automatically.

No `reset` command in v1a — there is no write-limit mechanism to reset (see `dish submit`, Step 5); a
`consumed`/`discarded` submission is simply not reusable, and a fresh `dish start` is how an editor
gets a new attempt.

### Tests (`tests/test_dish_admin_recover.py`, `tests/test_dish_admin_discard.py`)

- `recover` sets the submission to the status Marco passes (`ready` or `consumed`);
- `recover` on a submission not in `in_flight`/`uncertain` is rejected (nothing to recover);
- `discard` releases only `drafting`, `awaiting_verification`, or `ready` submissions, logs its reason,
  never mutates Asana, and rejects `in_flight`, `uncertain`, or terminal states;
- `dish-admin` is not reachable through the `dish` binary under any flag or subcommand name.

## Step 7 — generic-CLI advisory integration and managed-task registry

In `asana` (existing file): before `set-notes`/`append`/`replace`/batch note-updating operations/`raw`
writes touching `notes`/`html_notes`, call a new `dish_lib.is_managed(task_gid)` that compares the
task's current section GID directly against `COOKING_SOURCING_SECTION_GID`/
`COOKING_REFERENCE_SECTION_GID` — hardcoded constants in `dish_lib.py`, same convention as
`$PROTOCOL_MD_PATH`'s default. Resolved once by hand via `asana sections 1215089183018968`, not
re-resolved by name at runtime; the actual GIDs are `1215097887456673` (`Sourcing`) and
`1215259129474846` (`Reference`), confirmed live. Re-resolving by *name* on every invocation
would mean a rename of `Sourcing`/`Reference` silently breaks the lookup and misclassifies that
section's own tasks as newly managed — exactly the rename fragility the design's GID-based approach
exists to avoid, since a task's *current* section is always compared by GID, but the exclusion-set GIDs
themselves would otherwise be re-derived from a mutable name each run. Any task's section GID that
matches neither pinned value defaults to managed; an unresolved section (API error, missing data)
also defaults to managed (fail closed). If managed, `log_event` an advisory bypass event (task GID,
command, agent if passed via a new optional `--agent` flag on these commands); the write proceeds
unchanged in v1a. No blocking logic is added in this step — that's v1b, out of scope here.

### Tests (`tests/test_asana_advisory_logging.py`, `tests/test_dish_registry.py`)

- a note-write to a task in a non-`Sourcing`/non-`Reference` section logs an advisory bypass event and
  still succeeds;
- a note-write to a task in `Sourcing`/`Reference` does not log a bypass event;
- an unresolvable section (API error, missing section data) is treated as managed (fail closed) and
  logs an advisory event;
- a `Sourcing`/`Reference` section rename does not change managed status: with the pinned GID
  unchanged, a task in that section (now under a different display name) still compares equal by GID
  and is not logged as an advisory bypass;
- non-note operations (rename, move, complete, other fields) never log a bypass event, in both this
  step and after v1b (documented as a fixed invariant, not something v1b changes);
- `raw` writes containing `notes`/`html_notes` in the body are caught by the same check; `raw` writes
  that don't touch notes are not.

## Step 8 — logging/observability summary

A checked-in `.sql` file (`~/ai-tools/bin/dish-reports.sql`), not a `dish-admin report`
subcommand, decided — answering the bullet points in `dish-tool.md`'s Logging and
observability section: call counts by agent/submission kind/change-level, validation-failure rate by
rule, rejection rate (including repeated-rejection-on-same-task rate), and advisory-bypass count by
task/agent. The fuller query list (`small`-declared diff-size distribution, write/reset frequency) is a
v2 candidate tied to mechanisms not built in v1a either (diff-summary computation, write-count
escalation — see `dish-tool-future.md`). Run with `sqlite3 ~/ai-tools/var/dish-tool.db <
dish-reports.sql` when you're ready to decide v1b timing. A real command surface to build and test
would be overkill for what's fundamentally a handful of `SELECT`s; cheap to promote to a subcommand
later if it ends up being run often.

### Tests

One test per summary query against a seeded `audit_events`/`submissions` fixture, asserting correct
counts — whether built as a subcommand or shipped as a `.sql` file. A `.sql` file is still Python-
testable: load it and run each statement through `sqlite3` against the fixture DB in
`tests/test_dish_reports.py`, same as any other query; being a checked-in `.sql` file rather than a
subcommand doesn't make its output less important to verify.

## Step 9 — docs

- **`~/ai-tools/bin/dish-tool.md`** — no content change needed; this plan implements what
  it already specifies. Do not duplicate its content into this plan or vice versa.
- **ChatGPT workflow** — no new code; the local-agent-on-ChatGPT's-behalf procedure in
  `dish-tool.md`'s ChatGPT workflow section is already fully covered by Steps 2–5's
  `--agent gpt` routing. See Deployment for the runbook-pointer push this still requires. Replacing
  the manual relay with a custom GPT Action is a v2 candidate — see
  `dish-tool-future.md`, not built or open here.
- **`requirements.txt`** — no new entries; the manifest is JSON, parsed with stdlib `json` only.
- **`.gitignore`** — no separate action here; `var/` is added in Step 1 alongside the SQLite schema
  work, not deferred to this step.

## Deployment

Once all steps are built and merged, and the `--agent gpt` routing (Steps 2–5) is live: add the ChatGPT
runbook pointer bullet to `~/honest-pantry/cooking-master-reference.md`'s CORE section (the git-tracked
snapshot of the live Asana "Cooking master prompt" task ChatGPT actually reads), near the existing
readiness-gate bullet that already makes the same kind of hand-off — a short pointer, since that file
explicitly scopes itself to live execution only and defers task construction/verification to
`dish-protocol.md` "when it's actually needed." Adding the bullet is a `~/honest-pantry` edit, out
of scope for this ai-tools plan itself.

Then push it live via `asana set-notes 1215259129474847 -`, per that file's own sync instructions.

## Out of scope for this plan

Everything `dish-tool.md` defers to v1b or v2 (enforcement flip, two-failed-pass stop,
small-change speed bump, dependency surfacing, exact-content binding/hashing) or drops outright
(verifier-file hash gating, `--confirm-independent-review`, cached `managed_tasks` table, adversarial
self-review, cryptographic identity). Also out of scope: migrating existing tasks' content to the
canonical structure, the post-three-way-split multi-file resolver (see the upstream-dependency note at
the top of this plan), and anything not already named in `dish-tool.md`'s Out of scope section.
