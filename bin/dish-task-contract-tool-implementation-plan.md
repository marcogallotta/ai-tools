# Dish task contract tool — v1a implementation plan

Scope: v1a only, per `dish-task-contract-tool.md`'s Versioning plan — the full guarded path
(`start`/`prepare`/`approve`/`reject`/`submit`/`contract-admin recover`/`contract-admin reset`),
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
  "process_record_subheadings": ["Decisions", "Research basis", "Material changes", "Post-cook actuals"]
}
```

`manifest_version` lets the validator detect a future incompatible manifest shape without guessing.
This is a proposed starting shape, not a final one — you should review it against the actual current
heading set before approving, since I derived it from the prose rather than from a live task sample.
`process_record_subheadings` should additionally be scoped as valid only nested under `PROCESS RECORD`,
not as a second top-level allowlist — not yet reflected in the JSON above pending the scope question
below.

**OPEN — manifest scope.** The change plan's own wording asks for a manifest of "headings, required
fields, and allowed values" — broader than the heading-only shape drafted above. The design doc's
Deterministic validation checks list, though, treats `Stage:`/`Human review:`/`Verification:`/
`Self-verified:` as fixed field names the validator checks directly, and only describes the *heading*
allowlist as manifest-sourced — expanding the manifest to also carry those fields' allowed-value
vocabularies (e.g. `Stage: Draft|Researched`) is new scope beyond what that checklist currently
specifies, not a gap in this plan. Keep the manifest heading-only (matching the design doc as written),
or expand it to also cover required-field names and allowed values (closer to the change plan's literal
wording, more validator coverage, more surface to keep in sync)?

**OPEN — manifest encoding.** You asked whether YAML/JSON are hard for agents to parse specifically,
and whether that nervousness is founded. It isn't, for the reason you were worried about: the block is
parsed by the Python validator (`json.loads`, two lines, stdlib-only), not read structurally by an
LLM — and LLMs handle fenced JSON/YAML at least as reliably as free-form prose lists when they do read
it. The real tradeoff is parser strictness: JSON fails loudly on a malformed edit (missing bracket/
comma), matching this design's fail-closed philosophy elsewhere (e.g. unresolved section-GID → managed
by default). YAML is more pleasant to hand-edit but indentation-sensitive, so a careless prose edit
near the block could silently misparse rather than error. A hand-parsed plain markdown list avoids new
syntax entirely but reintroduces exactly the brittle-regex risk the design explicitly rejected for the
allowlist. **Recommendation: fenced JSON, as drafted above.** Flag if you'd rather have YAML for
editability — I'll add PyYAML as a dependency if so.

**2. `Self-verified: <agent>, <date>` as a required process-record line.** Add it alongside `Stage:`,
`Human review:`, and `Verification:` (dish-task-contract.md:86–88), stating it attests the editor's
own end-to-end self-review scoped by change class before submission — the human-facing description of
what `contract prepare`'s mechanical check (`dish-task-contract-tool.md` Workflow §1) already enforces
byte-for-byte.

**3. Statement that contract-managed note writes go through the guarded tool.** One sentence near
`## Canonical task` or `## Workflow` naming `dish-task-contract-tool.md`'s `contract` command as the
intended path for managed tasks, and noting that v1a logs but does not yet block direct edits (so the
contract text doesn't overclaim enforcement it doesn't have yet).

Exact final wording for items 2 and 3 is yours to set; I've described intent, not the sentence, since
this is contract prose you own.

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

**OPEN — `$CONTRACT_MD_PATH` location.** honest-pantry is a sibling directory to `ai-tools`, not a git
submodule of it, so the tool reads it as a plain filesystem path with its own git repo underneath (for
`contract_revision()`'s `git log`). **Recommendation: default `~/honest-pantry/dish-task-contract.md`,
overridable via env var**, exactly as drafted above — flag if honest-pantry could ever move or if you
want this pinned some other way.

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
`approve`/`reject`/`submit`, `choices=` for `--agent`/`--change-level`). No convention forces this to
match `asana`'s hand-rolled flag parsing, and `argparse` gets free `--help`, error messages, and
`choices` validation for no extra code.

`c_start(...)` implements Workflow §1: task existence check; open-submission check (app-level + relies
on Step 1's unique index on `submissions(task_gid)` for non-terminal `status`, including `drafting`,
for the race case — this is the lock); one baseline read of the live task, recording
`baseline_modified_at` and `baseline_notes_hash` via `contract_lib.canonicalize_and_hash`; creates one
`submissions` row, status `drafting`, `write_count` 0; prints the `submission_id` back to the caller —
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
life; the full deterministic validation rule set (readiness line, `WHAT TO BUY` presence, process-record
lines well-formed, `Self-verified:` agent match, change-level/process-record consistency,
contract-revision match, heading allowlist, readiness-contradiction check); on a validation failure, the
submission stays in `drafting` (it already exists, opened by `start`) and the attempt is logged; on a
pass, diff-summary computation (`characters_added`, `characters_removed`, `lines_changed`,
`headings_touched`) against the baseline captured at `start`, and advancing the row out of `drafting`
(`ready` for `small`, `awaiting_verification` with `required_verifier_family` set for `medium`/`large`).

Every validation failure is reported with every violated rule, not just the first, and logged via
`log_event` even on a failing attempt, since `start` already created the row it attaches to.

**OPEN — attempt-number logging.** The design doc's Logging and observability section requires `prepare`
logging to record "whether the note passed validation on the first attempt, and if not, which attempt
number succeeded" — this needs a correlation key grouping a task's failed `prepare` attempts (which get
no submission row) with the eventual successful one, e.g. a counter keyed on `(task_gid, editor_agent)`
reset once a submission is created. It's a small addition, not heavy machinery, but it is a design-doc
logging requirement this plan hadn't accounted for. Build it as specified, or drop it and log failure
counts by task/rule only (simpler, but under-delivers what the design doc's Logging section already
promises, so the design doc would need the matching edit)?

### Tests (`tests/test_contract_prepare.py`, `tests/test_contract_validation.py`)

Mirrors `dish-task-contract-tool.md`'s Testing requirements section directly:

- each deterministic-validation rule fails and passes independently (one test per rule, not one
  mega-test), including the readiness-contradiction rule's three trigger conditions;
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
- diff summary fields are computed and logged, and are not persisted as a second full copy of the note
  text anywhere in `submissions` or `audit_events`;
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

`c_submit(...)` implements Workflow §4: load submission, require `ready`; reject outright, with no
Asana call attempted, if `write_count` has already reached its current limit (`2 + reset_count`) — the
agent is told to stop and ask Marco, only `contract-admin reset` unblocks it; if `write_count` is 1 and
`--final` was not passed, make no Asana call, do not increment `write_count`, and return a hard
confirmation prompt (idempotent — repeating the plain call just repeats the prompt); recompute file hash
and reject on mismatch; fresh task read, compare `modified_at` to `baseline_modified_at`, reject (mark
`stale`) on any difference; atomic `ready` → `in_flight` flip; increment `write_count` — this happens for
every attempt that reaches an actual Asana mutation, regardless of outcome (success, confirmed failure,
or uncertain), with no special case for a retry of identical content vs. a genuinely different edit; one
notes update call; on clear success mark `consumed`.

Failure classification is by status code, not "any mapped `ApiException` means confirmed failure" — the
design doc's own Failure behaviour section requires the tool to *know* the write wasn't applied before
reverting to `ready`, which a 5xx doesn't establish:

- **Confirmed non-application → `ready`** (safe to retry as-is): `400`/`401`/`403`/`404` (request
  rejected before any mutation could occur), and `429` once the SDK's own retry/backoff is exhausted
  (rate-limited means the request was never accepted for processing).
- **Uncertain → `uncertain`, resolved only via `contract-admin recover`**: `5xx` after the SDK's own
  retries are exhausted (a 500/502/503/504 does not prove the mutation was never applied), timeouts, and
  connection breaks with no response at all.

### Tests (`tests/test_contract_submit.py`)

- content-hash mismatch at `submit` rejected, no Asana call made;
- any `modified_at` drift rejected and marks `stale`, no Asana call made;
- exactly one Asana mutation call on the success path, none on any pre-write failure path;
- each of `400`/`401`/`403`/`404`/`429`-after-retries reverts `in_flight` → `ready`, and the same
  submission can be retried;
- each of `5xx`-after-retries, a simulated timeout, and a connection break moves to `uncertain`, not
  silently retried and not left `in_flight` forever, and not misclassified as confirmed-`ready`;
- two simultaneous `submit` calls on the same submission: only one can flip `ready` → `in_flight`
  (assert on the second call's rejection, not just "doesn't crash" — this is the same
  race shape as `start`'s open-submission check, but at a different table state);
- `consumed`/`stale`/`rejected` cannot be resubmitted, even byte-identical;
- `write_count` gating: write 1 executes silently with no confirmation required and increments
  `write_count` to 1; a second plain `submit` call (no `--final`) makes no Asana call, does not
  increment `write_count`, and returns the confirmation prompt every time it's repeated; `submit
  --final` at that point executes the 2nd write; a third attempt (with or without `--final`) is
  rejected before any Asana call is attempted;
- a submission at its write limit is unusable until `contract-admin reset`; after reset, exactly one
  further write is available and requires `--final` immediately — a plain `submit` call post-reset does
  not execute and does not itself consume the granted write.

## Step 6 — `contract-admin recover` / `contract-admin reset`

`~/ai-tools/bin/contract-admin` (new, separate executable — deliberately not a hidden subcommand of
`contract`, per the design's "distinct binary/subcommand namespace" requirement). `recover
<submission-id>` performs one live read of notes + `modified_at`, and applies the four-row outcome
table from Workflow → Uncertain API outcome / crashed process exactly.

`reset <submission-id>` clears a submission that has hit its write limit (`write_count` at
`2 + reset_count`, see `contract submit`) so the same validated content can be written again:
increments `reset_count` by one, which raises the write-limit ceiling by exactly one further write — not
a restored two-write budget — and that next write requires `--final` immediately (no silent write, no
confirmation round-trip the second time around, per Workflow §4). `reset` only releases the exhausted
row's hold; it does not reopen the row for further drafting — a further edit still requires a fresh
`contract start`. Rejected if the submission is not currently at its write limit (nothing to reset).

### Tests (`tests/test_contract_admin_recover.py`, `tests/test_contract_admin_reset.py`)

- one test per outcome-table row (four cases): intended-hash-match (either `modified_at` state) →
  `consumed`; baseline-hash-match + unchanged `modified_at` → `ready`; baseline-hash-match + changed
  `modified_at` → `stale`; neither hash matches (either `modified_at` state) → `stale`;
- `recover` on a submission not in `in_flight`/`uncertain` is rejected (nothing to recover);
- `reset` on a submission not currently at its write limit is rejected;
- `reset` increments `reset_count` by exactly one and grants exactly one further write; the next
  `submit` call on that submission requires `--final` immediately — no silent write, no confirmation
  round-trip;
- a second `reset` after the granted write is again consumed raises the ceiling by one more, not back to
  a two-write budget;
- `contract-admin` is not reachable through the `contract` binary under any flag or subcommand name.

## Step 7 — generic-CLI advisory integration and managed-task registry

In `asana` (existing file): before `set-notes`/`append`/`replace`/batch note-updating operations/`raw`
writes touching `notes`/`html_notes`, call a new `contract_lib.is_managed(task_gid)` that compares the
task's current section GID directly against `COOKING_SOURCING_SECTION_GID`/
`COOKING_REFERENCE_SECTION_GID` (config values, resolved by name once, by hand, at setup time and
pinned — not re-resolved by name on every process start). Re-resolving by *name* on every invocation
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

A `contract-admin report` command (or a plain SQL query script, if you'd rather run it ad hoc — see
**OPEN** below) answering the six bullet points in `dish-task-contract-tool.md`'s Logging and
observability section: call counts by agent/change-level, validation-failure rate by rule, rejection
rate and repeated-rejection-per-task rate, `small`-declared diff-size distribution, advisory-bypass
count by task/agent, and how often the `--final` confirmation is actually reached vs. write 1 alone
being sufficient, and how often `contract-admin reset` is actually needed.

**OPEN — how you want to consume this.** A `contract-admin report` subcommand is more discoverable and
keeps this consistent with the rest of the tool, but it's a real command surface to build and test for
what's fundamentally a handful of `SELECT`s you might just as easily run yourself against the sqlite
file when you actually want the v1a→v1b answer. **Recommendation: skip the subcommand for v1a; ship the
six queries as a checked-in `.sql` file (`~/ai-tools/bin/contract-reports.sql`) you run with `sqlite3
~/ai-tools/var/dish-contract.db < contract-reports.sql` when you're ready to decide v1b timing.** Cheap
to promote to a real subcommand later if you end up running it often. Flag if you'd rather have the
subcommand now.

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
  `--agent gpt` routing. Worth a short runbook note wherever Marco keeps ChatGPT-facing instructions
  (**OPEN** — I don't know if such a place exists; flag if it does and I'll add a pointer there instead
  of leaving this as prose only here).
- **OPEN — is the current ChatGPT process itself the right shape.** Marco flagged the present
  ChatGPT-side workflow (relaying context in/out manually, per that section) as jank in practice.
  Marco already has a custom GPT hosted on his laptop for another purpose, so the groundwork (Action
  endpoint, laptop hosting, schema registration) is proven, not hypothetical — extending that same
  pattern with a couple of endpoints wrapping task-read/`contract` calls behind a bearer token is a
  known-cost addition, not a new infra bet. What's actually open is a judgment call, not a build
  question: whether ChatGPT should be calling directly into a live endpoint against real tasks (vs.
  today's model where nothing executes until a local agent runs `contract prepare`/`submit`), and
  whether `Self-verified:` should stay something ChatGPT asserts in its own output rather than
  something the Action layer stamps on its behalf.
- **`requirements.txt`** — no new entries if Step 0 lands as JSON (stdlib `json` only); add `pyyaml` if
  you choose YAML instead.
- **`.gitignore`** — add `var/` for the new SQLite DB location.

## Out of scope for this plan

Everything `dish-task-contract-tool.md` defers to v1b or v2 (enforcement flip, two-failed-pass stop,
small-change speed bump, dependency surfacing, token/submission replacement) or drops outright
(verifier in-place editing, `--confirm-independent-review`, cached `managed_tasks` table, adversarial
self-review, cryptographic identity). Also out of scope: migrating existing tasks' content to the
canonical structure, and anything not already named in that document's Out of scope section.

## Open questions summary

1. Manifest encoding (JSON vs. YAML vs. plain list) — recommendation: JSON, drafted in Step 0.
2. Manifest scope — heading allowlist only (matches design doc's validation list as written), or
   expanded to also cover required-field names and allowed values (matches the change plan's literal
   wording, more coverage, more surface). See Step 0.
3. Exact final wording for the `Self-verified:` and "writes go through this tool" contract-text
   additions — yours to set; intent described in Step 0.
4. `$CONTRACT_MD_PATH` default — recommendation: `~/honest-pantry/dish-task-contract.md`, overridable.
5. Attempt-number logging — build the `(task_gid, editor_agent)` attempt counter the design doc's
   Logging section already requires, or drop it and simplify the design doc to match? See Step 3.
6. Logging/observability surface — recommendation: a checked-in `.sql` file, not a new subcommand, for
   v1a.
7. Where (if anywhere) a ChatGPT-facing runbook pointer should live.
8. Whether to replace the manual ChatGPT copy/paste relay with a custom GPT Action (Marco already
   has the same laptop-hosted groundwork proven for another purpose); the open call is
   trust/semantics — direct live-endpoint access to real tasks, and whether `Self-verified:` stays
   ChatGPT's own assertion rather than something the Action stamps on its behalf — not build effort.
   See Step 9.
