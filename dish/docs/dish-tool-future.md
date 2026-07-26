# Dish Tool — Version Triage / Future Ideas

**Purpose:** Sort work that is not in the current v1 design into the first real post-v1 release and
later work that still needs usage evidence or additional design. `dish-tool.md` remains the v1
design; bounded additions still worth considering before V1 is frozen now live at the end of
`dish-tool-imp.md`.

**Status:** This is design triage, not implementation authorization. Building any item still
requires Marco's explicit decision.

## Versioning and rollout

The tool is built and rolled out against the evidence in `dish-docs-design.md`,
`dish-incident-log.md`, and `dish-review-log.md`.

V1 ships against the three-way split. The approved checked-in `dish-planning-protocol.md`,
`dish-research-protocol.md`, and `dish-verification-protocol.md` are repository-maintained governing
sources, not generated or synced from Asana. The V1 resolver and pre-authorship binding are specified
in `dish-tool.md`. One human-readable `protocol_release` identifies their exact checked-in set
together with the manifest/schema. The shared `git-commit` wrapper owns the version file: agents and
humans do not edit it; the wrapper advances and includes it atomically whenever a file in the defined
protocol-governed set changes, rejects direct edits or an unversioned protocol-governed commit, and
ignores unrelated commits. Git history supplies exact-content binding for that governing bundle, so
the release value need not be a combined hash. `tool_version` remains separate; compatible tool-only
fixes change only that version, while schema or semantic compatibility changes require both versions
to advance.

**V1 ships as one release.** The full guarded path (`create` / `read` / `start` / `prepare` /
`approve` / `reject` / `submit` plus `dish-admin recover` / `discard` / `unblock`) is implemented,
tested, and usable end-to-end against live tasks, performing real Asana writes through the guarded,
single-use submission path.

The earlier v1a/v1b split staged one thing — a managed-task guard in the generic Asana CLI, advisory
first and blocking later. Both the split and the guard are dropped: the guard covered only the local
CLI agents, which already prompt Marco before any Asana write, and never ChatGPT, which writes
through its own Asana integration. Drift detection carries that protection instead, and catches what
the guard could not.

## Earmarked: v2 — first release soon after v1

V2 adds a small content-sensitive layer on top of v1's controlled structural path and removes the
manual relay between ChatGPT and the tool. It should begin only after V1 is proven in real use. The
items below have either recorded incident evidence, an
approved bounded direction, or a proven implementation pattern; they do not turn the tool into a
semantic recipe judge or a general remote platform.

### Small-change carelessness speed bump

Address Marco's concern about an honest agent carelessly declaring a material edit `small`, not a
malicious caller gaming the trusted identity model. Use the diff telemetry considered in
`dish-tool-imp.md`, if selected, to choose a narrow deterministic trigger from observed `small`
changes. If it was not selected for v1, gather that evidence before choosing a trigger. The
protection remains a speed bump, not independent verification for every small edit and not
inferred semantic classification.

The trigger, the exact acknowledgement required, and warning-versus-blocking behaviour remain open
until that evidence exists. No implementation should guess them in advance.

### Bounded direct-dependency surfacing

Surface only bounded direct candidates: exact task-GID references, explicit Asana links, exact task
name references, and clearly named planning documents. This responds to the review log's four
change-closure catches across five artifacts.

The result is advisory and never blocks validation, readiness, or submission. The scanner does not
recurse, decide semantic impact, or require a disposition merely to issue or consume the single-use
submission. A candidate-disposition format can be added only if later use shows it is helpful.

### Three-value nutrition grammar and enforcement

Add one narrow canonical syntax for calories, protein, and fat per complete served portion, including
stated sides. Enforce 750-1,000 kcal, over 40 g protein, and under 40 g fat unless the matching
Planning exemption tag and recorded Marco approval cover the departure. Incidents 5 and 23 provide
the protected outcome and Marco-approved limits.

Do not add carbohydrate parsing, 4/4/9 reconciliation, warning tolerances, or broader nutrition
judgment. The exact field syntax must be approved in the protocol manifest before implementation;
that protocol/schema addition is why this remains v2 rather than being casually folded into v1.

### Candidate-file binding across the guarded handoff

Bind the exact candidate accepted at `prepare` or `approve` to the bytes supplied at `submit`, for
example with a stored cryptographic digest. This closes only the local approval-to-submit handoff:
`submit` rejects a different file instead of trusting that the controlled handoff supplied the same
one. It does not claim to detect edits made directly in Asana.

Keep this separate from live-task baseline checking below. If v1 usage shows no handoff problem and
the mechanism would complicate correction/retry state materially, it can remain future without
holding up the other v2 items.

### ChatGPT Action / remote endpoint

Make direct ChatGPT access to the bounded `dish` interface a v2 deliverable. It removes Marco's
recurring manual script and copy/paste relay rather than adding a merely optional integration.
The infrastructure pattern is already proven: `~/plant-monitoring/backend/app/routers/assistant.py`
was hosted on Marco's laptop and exposed through Tailspin to ChatGPT via GPT Actions. Reuse that
pattern instead of treating hosting as a new platform project.

The endpoint exposes dish operations, not raw Asana access, and preserves the v1 state machine and
validation boundary. A cook still receives the complete signed above-divider brief, and a verifier
the complete candidate and provenance. Before implementation, settle only the narrow trust
semantics introduced by direct calls: how the authenticated Action maps to the acting ChatGPT role
and what `Self-verified:` asserts when the manual relay is gone.

## Future — not the first post-v1 release

### External-edit detection

Detecting web, integration, or other edits to the live task during a submission requires a saved
live baseline and an explicit conflict/recovery policy. V1 deliberately accepts this risk. Build it
only if v1 usage shows external edits or live-state races are actually occurring; do not bundle it
with the smaller candidate-file binding merely because both may use hashes.

### `WHAT TO BUY` / `QUANTITIES` reconciliation

Incident 21 demonstrates the failure, but literal numeric equality is not the invariant. Automation
would first need a per-ingredient syntax that distinguishes recipe use, live stock, usable yield or
trim, package/minimum purchase quantity, and an explicit reason for differences. That is a larger
task-data model, not a narrow validator rule, so it follows rather than joins the first field-grammar
release.

### Scripted migration for later protocol releases

Perform deterministic structural transformations, remove obsolete fields, stamp the new
`protocol_release`, and stop on content requiring judgment rather than inventing it. This follows
the split's initial snapshot-backed, agent-led migration; it does not replace that rollout or claim
semantic equivalence. Build it when a later release creates a repeated migration need.

### Tool-mediated cooking

Route cooking agents through `dish` rather than letting them write to Asana directly. In this
rollout they write cook-log entries (Asana comments) and never touch the task body, which keeps
signoff safe without needing a cooking command surface. Bringing cooking inside the tool is the
prerequisite for the database backend below: once every agent path goes through `dish`, the backend
can change without touching any agent workflow. Design then: the cook-log write command, how a
Marco-granted override is recorded as a first-class operation, and whether cooking reads need
anything beyond `dish read`.

### Database backend and separate frontend

Replace Asana with a database-backed store and a separate human-facing frontend. The stable `dish`
interface remains the agents' only interface, so the backend change does not alter their workflow or
expose storage-specific operations. It may also remove Asana API and network latency, with the
clearest potential speed gain for local Codex and Claude agents accessing the backend on the same
machine or local network. Preserve the guarded state machine, validation, audit history, and safely
retryable writes while migrating the live corpus and reconciling cutover state. Design the frontend
around Marco's task-reading and intervention needs rather than reproducing Asana's general
project-management model.

### Usage-triggered recovery and write machinery

None of the following answers a recorded incident. Build an item only when v1's audit log shows its
specific simpler behaviour is insufficient:

- token/submission replacement distinct from `dish-admin recover`;
- a deterministic crashed/uncertain-outcome recovery table rather than Marco's inspected outcome;
- `write_count` escalation, a `--final` confirmation round-trip, or `dish-admin reset`; and
- fuller periodic summaries for mechanisms that have actually been selected.

## Dropped, not deferred

These were considered and rejected outright, not placed in any future release:

- `--confirm-independent-review` and dedicated self-verification-collision detection. **Rationale
  void as of `dish-tool-update.md`:** the original reasoning depended on opposite-family routing
  making same-family approval structurally unreachable, but the protocol-compatibility update
  removes opposite-family routing entirely (verification requires a fresh independent run by any
  agent that did not construct or materially edit the candidate, not "the opposite family" of the
  editor). The residual dishonest-declaration risk is still outside
  the trusted-identity model either way, so the drop itself still stands — but if this is
  reconsidered later, reconsider it on that basis, not the original one.
- A cached authoritative `managed_tasks` table. Management remains live-resolved.
- A distinct adversarial self-review mechanism. The review log records that it was an assistant
  recommendation and was not approved; exact-source review and fresh independent verification remain.
- Cryptographic agent authentication, recursive dependency audits, and a general-purpose remote or
  multi-user trust service beyond the bounded ChatGPT Action. No evidence changes their out-of-scope
  status.
