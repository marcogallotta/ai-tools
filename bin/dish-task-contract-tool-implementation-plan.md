# Dish task contract tool — v1a implementation plan

Scope: v1a only, per `dish-task-contract-tool.md`'s Versioning plan — the full guarded path
(`start`/`prepare`/`approve`/`reject`/`submit`/`contract-admin recover`),
soft-launched with the generic Asana CLI's managed-task check running advisory/log-only. v1b's
enforcement flip and all v2 items are out of scope here; nothing in this plan builds toward them ahead
of need.

This plan assumes the design in `dish-task-contract-tool.md` as final for v1a. Where that document
already resolved a question, this plan does not re-litigate it — it cites the resolution and moves to
what building it requires. Genuinely open implementation judgment calls are marked **OPEN** with
options and a recommendation; nothing else in this plan should be read as still undecided.

## Rollout

Ship as staged commits: Step 0 → Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 → Step 7 → Step
8 → Step 9. Each stage lands and is independently testable. Steps 1–6 can be built and tested against a
fixture copy of the contract text before Step 0 is merged; nothing in Steps 1–6 depends on Step 0
having landed in the real `dish-task-contract.md`, but the tool must not soft-launch against live tasks
(Step 7 going live) until Step 0 is merged for real, since `prepare` cannot validate against a manifest
that doesn't exist yet.

See Deployment for the post-build go-live steps.

## Step 0 — contract-text prerequisites (draft here, approve separately)

`dish-task-contract-tool.md`'s validator assumes three things in `dish-task-contract.md` that are not
there yet (confirmed by reading the live file). This is drafted here for your sign-off per the change
plan's "Approval package required before production changes" — it is a contract-text edit, which is
explicitly out of scope for the *tool* itself, but it's a hard precondition for the tool to work at
all, so the plan can't skip past it.

**Already resolved, no change needed:** `contract <revision>` — the contract text (line 183) already
defines this as "the latest Git commit that changed this file. Without Git access, use the SHA-256 of
the exact contract text used plus the date and time it was read." The tool implements exactly this
(see Step 1, Content hashing/canonicalization); no contract-text addition required.

**1. Machine-readable canonical-structure manifest.** Add a fenced block near `## Canonical task`
(dish-task-contract.md:55) enumerating the allowed headings and which are required, sourced from the
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
validation checks list (`dish-task-contract-tool.md`:216–229) already settles this downstream: it
treats `Stage:`/`Human review:`/`Verification:`/`Self-verified:` as fixed field names the validator
checks directly, and describes only the *heading* allowlist as manifest-sourced. The change plan
doesn't need to be brought back in sync with this — see `CLAUDE.md`'s authority-flow note. Heading-only,
matching the JSON above.

**Manifest encoding: fenced JSON, decided.** The block is parsed by the Python validator
(`json.loads`, two lines, stdlib-only), not read structurally by an LLM, so agent-parsing difficulty
isn't a real concern either way. JSON fails loudly on a malformed edit (missing bracket/comma),
matching this design's fail-closed philosophy elsewhere (e.g. unresolved section-GID → managed by
default) — the deciding factor over YAML (pleasant to hand-edit but indentation-sensitive, so a
careless prose edit near the block could silently misparse rather than error) or a hand-parsed plain
markdown list (avoids new syntax but reintroduces the brittle-regex risk already rejected for the
allowlist).

**2. `Self-verified: <agent>, <date>` as a required process-record line.** Add it as a fourth line in
the `## PROCESS RECORD` block (dish-task-contract.md:86–88), alongside `Stage:`, `Human review:`, and
`Verification:`:

```text
Self-verified: <agent>, <date>
```

Drafted wording for the explanatory sentence, matching the terse style of the surrounding contract
prose (no other field in this block gets its own paragraph either, beyond the dedicated Verification
and readiness section for `Verification` specifically) — one line is enough here:

> `Self-verified` attests the editor's own end-to-end self-review of the note, scoped by change class,
> immediately before submission; the named agent must match the note's actual editor.

**3. Statement that contract-managed note writes go through the guarded tool.** Drafted wording, for
one sentence near `## Canonical task` or `## Workflow`:

> Contract-managed task writes go through `dish-task-contract-tool.md`'s `contract` command; as of
> v1a this is logged, not yet enforced — a direct edit still succeeds but is recorded as an advisory
> bypass event.

Both are drafts against the actual contract prose style, ready for you to approve as-is or edit —
not a placeholder for you to write from scratch.

## Step 1 — foundation: shared modules, schema, hashing, manifest parsing

**`contract_lib.py`'s own Asana client, not an `asana_lib.py` extraction yet.** Give `contract`/
`contract-admin` their own small client/auth/error-handling helpers inside `contract_lib.py` (same
shape as `asana`'s `load_pat`/`client()`/`_error_detail`/`_call`, duplicated rather than shared for
now). Extracting a shared `asana_lib.py` out of the already-shipped `asana` CLI is a refactor of
production code the new feature doesn't need in order to work — deferring it keeps v1a build risk
contained to new files only. Revisit consolidation once v1a is stable and proven; `asana`'s existing
tests are unaffected either way.

**`~/ai-tools/bin/contract_lib.py` (new)** — everything `contract` and `contract-admin` share:

- SQLite connection helper, schema creation/migration, targeting `~/ai-tools/var/dish-contract.db`
  (create `~/ai-tools/var/` if absent; add `var/` to `.gitignore` alongside the existing
  `__pycache__/`/`.venv/` entries).
- `submissions` and `audit_events` tables exactly as specified in `dish-task-contract-tool.md`'s
  SQLite model, including the partial unique index on `submissions(task_gid)` for non-terminal
  `status`.
- Canonicalization + hashing: UTF-8, LF line endings, no trimming/whitespace cleanup/reordering,
  SHA-256, canonicalization version stored with the record — per Content hashing. One function,
  `canonicalize_and_hash(text) -> (canonical_bytes, hash_hex, version)`, used identically by
  `prepare`/`approve`/`submit`; no second implementation anywhere.

  **Verified against a live Asana write/read round trip**, task `1216683399494189` in the "test"
  project (`1216693403164366`; left in place, not deleted — harmless scratch data, not a real dish
  task):

  * No trailing-newline normalization — the raw API JSON confirmed Asana stores exactly the bytes
    sent, no added or stripped trailing newline. (An initial apparent mismatch was this CLI's own
    `print()` adding a display-time `\n`, not an Asana behavior — resolved by comparing against
    `asana raw GET` output instead of `asana notes`.)
  * CRLF is preserved literally, not normalized to LF — Asana does zero line-ending work. LF
    canonicalization is entirely the tool's own responsibility; nothing arrives pre-normalized.
  * Non-ASCII content (accented Latin, em-dash, curly quotes, CJK) round-trips byte-for-byte.
  * `modified_at` is bumped by *any* field change, not just notes — confirmed by renaming the task
    (touching only `name`) and observing `modified_at` move. It is a whole-task signal, not a
    notes-specific one. This matters only if something later compares `baseline_modified_at` against
    a fresh read: nothing in this design does that automatically today — `start` captures it once and
    only `content_hash` is actively re-checked (at `approve`/`submit`); `baseline_modified_at` is
    stored for Marco's manual investigation via `contract-admin recover`, not consumed by any
    automated gate. Do not add one later without accounting for this — an automated
    `modified_at`-equality check would misfire on routine unrelated activity (a rename, a section
    move, likely a comment or custom-field edit too).

  Conclusion: the proposed canonicalization (UTF-8, LF, no trimming/whitespace cleanup/reordering) is
  correct as designed — Asana does no content rewriting for `canonicalize_and_hash` to account for.
- `contract_revision()`: runs `git -C <honest-pantry path> log -1 --format=%H -- dish-task-contract.md`;
  on any git failure, falls back to `sha256(contract_text) + read timestamp`, matching the contract
  text's own fallback rule (dish-task-contract.md:183–184) exactly.
- Manifest loader: reads `dish-task-contract.md` (path from `$CONTRACT_MD_PATH`, default
  `~/honest-pantry/dish-task-contract.md`, mirroring `asana`'s `$ASANA_ENV` pattern), extracts and
  `json.loads`s the fenced manifest block added in Step 0, and returns `(canonical_manifest,
  contract_revision, contract_text_hash)`. A missing or malformed manifest block is a hard failure —
  no `prepare` can proceed — consistent with fail-closed elsewhere in this design.
- Agent family routing: `family(agent) -> "claude"|"gpt"`, per the `claude` / `gpt,codex` mapping.
- Change-level mapping: `small|medium|large -> Local|Delta|Reconstruction` for the process record.
- Audit logging: one `log_event(...)` function writing to `audit_events`, called by every command
  path in Steps 2–7, including failure paths with no submission row.

**`$CONTRACT_MD_PATH` default: `~/honest-pantry/dish-task-contract.md`, decided.** honest-pantry is a
sibling directory to `ai-tools`, not a git submodule of it, so the tool reads it as a plain filesystem
path with its own git repo underneath (for `contract_revision()`'s `git log`). Not actually a plan-level
recommendation — the design doc already states this exact default, overridable via env var
(`dish-task-contract-tool.md`:191), matching `asana`'s `$ASANA_ENV` pattern.

### Tests (`tests/test_contract_lib.py`, `tests/test_contract_schema.py`)

- schema creation is idempotent (running migration twice doesn't error or duplicate);
- partial unique index rejects a second non-terminal `submissions` row for the same `task_gid`, and
  permits a new row once the prior one is terminal (`consumed`/`stale`/`rejected`);
- `canonicalize_and_hash` is stable across repeated calls on identical input; a CRLF-only input and its
  LF-only equivalent normalize to the *same* canonical bytes and hash (LF normalization is part of what
  "exact" means, per Content hashing); any other byte difference that survives canonicalization produces
  a *different* hash;
- `contract_revision()` returns the git commit hash when git succeeds, and the SHA-256+timestamp
  fallback shape when git is unavailable (mock the subprocess call for both branches);
- manifest loader parses a valid fixture block correctly; rejects (does not silently accept) a missing
  block, a malformed-JSON block, and a block with an unrecognized `manifest_version`;
- `family()` maps `claude` → `claude`, `gpt`/`codex` → `gpt`, and rejects any other value;
- `log_event` writes a row with `submission_id` nullable and `task_gid` populated whenever known.

## Step 2 — `contract start`

`~/ai-tools/bin/contract` (new) — dispatch shell using `argparse` (subparsers for `start`/`prepare`/
`approve`/`reject`/`submit`, `choices=` for `--change-level`, and for `--agent` on every subparser
except `submit`, which takes no `--agent` — see the design doc's Agent identity and verifier routing
section). No convention forces this to match `asana`'s hand-rolled flag parsing, and `argparse` gets
free `--help`, error messages, and `choices` validation for no extra code.

`c_start(...)` implements Workflow §1: task existence check; open-submission check (app-level + relies
on Step 1's unique index on `submissions(task_gid)` for non-terminal `status`, including `drafting`,
for the race case — this is the lock); one baseline read of the live task, recording
`baseline_modified_at` and `baseline_notes_hash` via `contract_lib.canonicalize_and_hash`; creates one
`submissions` row, status `drafting`; prints the `submission_id` back to the caller —
this is the only token every later command (`prepare`/`approve`/`reject`/`submit`) operates on, there is
no separate token object.

This is the only baseline read for the submission's entire life. Closing the gap where a long drafting
window could otherwise silently miss an intervening edit was an open implementation question in an
earlier pass of this plan (previously drafted as an optional `contract start` addition); the design doc
now specifies `start` as a required first step for every submission, so that gap no longer exists —
there is nothing left to add here at v1a.

### Tests (`tests/test_contract_start.py`)

- `start` on a task with no open submission succeeds, creates a `drafting` row, and captures
  `baseline_modified_at`/`baseline_notes_hash` from a live read;
- `start` on a task with an already-open (non-terminal, including `drafting`) submission is rejected;
- two simultaneous `start` calls on the same task: only one succeeds (assert on the SQLite unique-index
  rejection for the second, not just "doesn't crash" — the race case Step 1's partial unique index
  exists for);
- `start` on a task that no longer exists is rejected before any row is created;
- every `start` call (pass or fail) produces exactly one `audit_events` row.

## Step 3 — `contract prepare` and deterministic validation

`c_prepare(...)` implements Workflow §2: confirms the submission exists and is in status `drafting` — it
does not take its own fresh baseline read, it uses `baseline_modified_at`/`baseline_notes_hash` already
captured on the row by `start`; manifest/revision/text-hash capture via `contract_lib`'s manifest loader,
stored on the row so this submission is validated against this exact frozen manifest for its entire
life; the full deterministic validation rule set (readiness line, `WHAT TO BUY` presence, `Portions:`
presence under `## QUANTITIES` when that heading is present, process-record lines well-formed,
`Self-verified:` agent match, change-level/process-record consistency, contract-revision match, heading
allowlist, readiness-contradiction check); on a validation failure, the
submission stays in `drafting` (it already exists, opened by `start`) and the attempt is logged; on a
pass, advancing the row out of `drafting`
(`ready` for `small`, `awaiting_verification` with `required_verifier_family` set for `medium`/`large`).
No diff-summary computation — dropped from v1a entirely (see `dish-task-contract-tool-future.md`).

Every validation failure is reported with every violated rule, not just the first, and logged via
`log_event` even on a failing attempt, since `start` already created the row it attaches to.

### Tests (`tests/test_contract_prepare.py`, `tests/test_contract_validation.py`)

Mirrors `dish-task-contract-tool.md`'s Testing requirements section directly:

- each deterministic-validation rule fails and passes independently (one test per rule, not one
  mega-test), including the readiness-contradiction rule's three trigger conditions;
- a note with `## QUANTITIES` present but no well-formed `Portions:` line under it fails; a note
  without `## QUANTITIES` at all is unaffected by this rule (heading omission stays the verifier's
  call, per the heading-allowlist rule above);
- `Self-verified:` missing, or naming an agent other than `editor_agent`, fails `prepare` — including a
  `gpt`-attributed submission. What's mechanically checked is only that a syntactically valid
  `Self-verified: gpt, <date>` line names the declared editor; the validator cannot determine whether
  ChatGPT itself produced that line or a local agent inserted it afterward, and no test should claim
  otherwise — that's a trusted-procedure rule (`dish-task-contract-tool.md`'s ChatGPT workflow section:
  a local agent does not add or backfill the line on ChatGPT's behalf), not a machine-enforceable one,
  consistent with the trusted-identity model in Scope;
- declared `--change-level` mismatched against the process record fails; initial construction is
  always treated as `large`;
- contract revision in the process record not matching the freshly-read `contract_revision` fails;
- heading outside the manifest allowlist fails; a heading present in the allowlist but absent from the
  note (and not `WHAT TO BUY`) does not fail, since omission-judgment is explicitly the verifier's job,
  not the validator's;
- `prepare` on a submission not in status `drafting` (nonexistent, or already past `drafting`) is
  rejected;
- `prepare` validates against the exact `baseline_modified_at`/`baseline_notes_hash` captured by
  `start`, not a fresh read of the task at `prepare` time;
- successful `prepare` produces the correct initial `status` for each of `small`/`medium`/`large`, and
  the correct `required_verifier_family` for `medium`/`large`;
- every `prepare` call (pass or fail) produces exactly one `audit_events` row.

## Step 4 — `contract approve` / `contract reject`

`c_approve(...)`: verifier-family check against `required_verifier_family`; exact content-hash match
against `content_hash` (hard reject on mismatch, no override, no path for the verifier to submit edited
content); on pass, an atomic conditional update — `UPDATE submissions SET status = 'ready', ... WHERE
submission_id = ? AND status = 'awaiting_verification'`, checking the row count affected — records
`verifier_agent`/`verifier_family`, sets `ready`. A zero-row update (status already moved by a
concurrent call) is reported as a conflict, not silently treated as success.

`c_reject(...)`: same family check as `approve`; same conditional-update pattern (`WHERE status =
'awaiting_verification'`) marking `rejected` (terminal); logs the reason. No in-place edit path — the
editor runs `contract start` again on the same task, then `contract prepare` on the fresh submission it
opens: a new lock, a new baseline, and a new submission, not a reopened one.

### Tests (`tests/test_contract_approve_reject.py`)

- verifier-family mismatch rejected on both `approve` and `reject`;
- content-hash mismatch at `approve` is a hard reject, and does not consume or mutate the submission;
- successful `approve` transitions `awaiting_verification` → `ready` and records verifier fields;
- `reject` transitions to terminal `rejected`; a subsequent `contract start` on the same task creates a
  new submission row (new lock, new baseline), not a reopened one;
- concurrent `approve`/`reject` on the same submission: only the first conditional update succeeds
  (row count 1); the second sees zero rows affected and is reported as a conflict, not applied on top
  of the first;
- `editor_agent == verifier_agent` is unreachable on `approve` — confirm the family check rejects it
  before any equality comparison would run, i.e. no separate collision-detection code path exists to
  test (per Versioning plan, Dropped).

## Step 5 — `contract submit` and failure handling

`c_submit(...)` implements Workflow §4: load submission, require `ready`; recompute file hash and reject
on mismatch; atomic `ready` → `in_flight` flip; one notes update call; on clear success mark `consumed` —
a submission is single-use, and a second `submit` call against an already-`consumed` submission is
rejected outright, with no write-count budget, `--final` confirmation step, or reset mechanism (no
incident evidences a need for one — see `dish-task-contract-tool-future.md`).

Note there is no pre-write freshness re-read here beyond the hash check: `start`'s exclusive lock means
no other `contract` CLI caller could have moved `modified_at` in the meantime (see the design doc's
Workflow §4 and Scope for the residual gap this doesn't cover).

Failure classification is by status code, not "any mapped `ApiException` means confirmed failure" — the
design doc's own Failure behaviour section requires the tool to *know* the write wasn't applied before
reverting to `ready`, which a 5xx doesn't establish:

- **Confirmed non-application → `ready`** (safe to retry as-is): `400`/`401`/`403`/`404` (request
  rejected before any mutation could occur), and `429` once the SDK's own retry/backoff is exhausted
  (rate-limited means the request was never accepted for processing).
- **Uncertain → `uncertain`, resolved only via `contract-admin recover`**: `5xx` after the SDK's own
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

### Tests (`tests/test_contract_submit.py`)

- content-hash mismatch at `submit` rejected, no Asana call made;
- exactly one Asana mutation call on the success path, none on any pre-write failure path;
- each of `400`/`401`/`403`/`404`/`429`-after-retries reverts `in_flight` → `ready`, and the same
  submission can be retried;
- each of `5xx`-after-retries, a simulated timeout, and a connection break moves to `uncertain`, not
  silently retried and not left `in_flight` forever, and not misclassified as confirmed-`ready`;
- two simultaneous `submit` calls on the same submission: only one can flip `ready` → `in_flight`
  (assert on the second call's rejection, not just "doesn't crash" — this is the same
  race shape as `start`'s open-submission check, but at a different table state);
- `consumed`/`stale`/`rejected` cannot be resubmitted, even byte-identical;
- a second `submit` call against an already-`consumed` submission is rejected outright — no write-count
  budget, `--final` confirmation step, or reset mechanism exists to test (see
  `dish-task-contract-tool-future.md`).

## Step 6 — `contract-admin recover`

`~/ai-tools/bin/contract-admin` (new, separate executable — deliberately not a hidden subcommand of
`contract`, per the design's "distinct binary/subcommand namespace" requirement). `recover
<submission-id> --status ready|consumed|stale` sets a stuck `in_flight` or `uncertain` submission's
status by hand, once Marco has checked the live task directly in Asana and confirmed what actually
happened — no automated outcome table; the mechanism to compute one from live notes-hash/`modified_at`
comparison is a v2 candidate with no evidenced need yet (see `dish-task-contract-tool-future.md`).

No `reset` command in v1a — there is no write-limit mechanism to reset (see `contract submit`, Step 5);
a consumed/stale/rejected submission is simply not reusable, and a fresh `contract start` is how an
editor gets a new attempt.

### Tests (`tests/test_contract_admin_recover.py`)

- `recover` sets the submission to the status Marco passes (`ready`, `consumed`, or `stale`);
- `recover` on a submission not in `in_flight`/`uncertain` is rejected (nothing to recover);
- `contract-admin` is not reachable through the `contract` binary under any flag or subcommand name.

## Step 7 — generic-CLI advisory integration and managed-task registry

In `asana` (existing file): before `set-notes`/`append`/`replace`/batch note-updating operations/`raw`
writes touching `notes`/`html_notes`, call a new `contract_lib.is_managed(task_gid)` that compares the
task's current section GID directly against `COOKING_SOURCING_SECTION_GID`/
`COOKING_REFERENCE_SECTION_GID` — hardcoded constants in `contract_lib.py`, same convention as
`$CONTRACT_MD_PATH`'s default. Resolved once by hand via `asana sections 1215089183018968`, not
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

### Tests (`tests/test_asana_advisory_logging.py`, `tests/test_contract_registry.py`)

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

A checked-in `.sql` file (`~/ai-tools/bin/contract-reports.sql`), not a `contract-admin report`
subcommand, decided — answering the four bullet points in `dish-task-contract-tool.md`'s Logging and
observability section: call counts by agent/change-level, validation-failure rate by rule, rejection
rate (including repeated-rejection-on-same-task rate), and advisory-bypass count by task/agent. The
fuller query list (`small`-declared diff-size distribution, `--final`/reset frequency) is a v2 candidate
tied to mechanisms not built in v1a either (diff-summary computation, write-count escalation — see
`dish-task-contract-tool-future.md`). Run with `sqlite3 ~/ai-tools/var/dish-contract.db <
contract-reports.sql` when you're ready to decide v1b timing. A real command surface to build and test
would be overkill for what's fundamentally a handful of `SELECT`s; cheap to promote to a subcommand
later if it ends up being run often.

### Tests

One test per summary query against a seeded `audit_events`/`submissions` fixture, asserting correct
counts — whether built as a subcommand or shipped as a `.sql` file. A `.sql` file is still Python-
testable: load it and run each statement through `sqlite3` against the fixture DB in
`tests/test_contract_reports.py`, same as any other query; being a checked-in `.sql` file rather than a
subcommand doesn't make its output less important to verify.

## Step 9 — docs

- **`~/ai-tools/bin/dish-task-contract-tool.md`** — no content change needed; this plan implements what
  it already specifies. Do not duplicate its content into this plan or vice versa.
- **ChatGPT workflow** — no new code; the local-agent-on-ChatGPT's-behalf procedure in
  `dish-task-contract-tool.md`'s ChatGPT workflow section is already fully covered by Steps 2–5's
  `--agent gpt` routing. See Deployment for the runbook-pointer push this still requires. Replacing
  the manual relay with a custom GPT Action is a v2 candidate — see
  `dish-task-contract-tool-future.md`, not built or open here.
- **`requirements.txt`** — no new entries; the manifest is JSON, parsed with stdlib `json` only.
- **`.gitignore`** — no separate action here; `var/` is added in Step 1 alongside the SQLite schema
  work, not deferred to this step.

## Deployment

Once all steps are built and merged, and the `--agent gpt` routing (Steps 2–5) is live: add the ChatGPT
runbook pointer bullet to `~/honest-pantry/cooking-master-reference.md`'s CORE section (the git-tracked
snapshot of the live Asana "Cooking master prompt" task ChatGPT actually reads), near the existing
readiness-gate bullet that already makes the same kind of hand-off — a short pointer, since that file
explicitly scopes itself to live execution only and defers task construction/verification to
`dish-task-contract.md` "when it's actually needed." Adding the bullet is a `~/honest-pantry` edit, out
of scope for this ai-tools plan itself.

Then push it live via `asana set-notes 1215259129474847 -`, per that file's own sync instructions.

## Out of scope for this plan

Everything `dish-task-contract-tool.md` defers to v1b or v2 (enforcement flip, two-failed-pass stop,
small-change speed bump, dependency surfacing, token/submission replacement) or drops outright
(verifier in-place editing, `--confirm-independent-review`, cached `managed_tasks` table, adversarial
self-review, cryptographic identity). Also out of scope: migrating existing tasks' content to the
canonical structure, and anything not already named in that document's Out of scope section.
