# Dish workflow and administration design

Decided product/workflow design, not yet implemented — same status as other `*-design.md`
documents referenced from [`future.md`](future.md). It defines how Human Review, safe reclaim,
Marco override, whole-version rollback, and connected-agent execution updates should behave.
It is not an implementation plan and does not authorize repository changes by itself.

This document is about the workflow and administration model, not about PostgreSQL or any other
storage engine. Some of it needs the durable-state and versioning capabilities described in
[`database-backend.md`](database-backend.md)/[`database-backend-imp.md`](database-backend-imp.md)
and [`postgresql-cutover.md`](postgresql-cutover.md) to be fully implementable; where that's true
it's noted, but this document does not depend on which backend eventually carries it.

Source: synthesized from an extended, iterative chat discussion (Marco's own ideas, refined over
many turns) rather than independently derived from the codebase. Treat it as Marco's current
intent, not as verified-correct requirements.

## Authority and workflow principles

- Marco is the final product authority. Agents may warn and record concerns, but may not impose
  substantive product or safety blocks on Marco.
- Only database mechanical-integrity and recovery conditions may block execution — never an
  agent's own judgment about whether a change is a good idea.
- Findings, evidence, proposals, authorization, and applied mutations are separate records or
  states. Approval is separate from application: a later eligible agent must be able to apply the
  exact approved change without depending on the original agent run.
- Discussion, clarification, urgency, silence, disagreement, and continued conversation are never
  authorization by themselves.

## Human Review lifecycle

Human Review findings move through: creation, evidence and confidence, proposed correction, open
question, Marco's answer, revision requested, approval, rejection, override, application, rollback,
closure.

Required guarantees:

- Marco's exact words are stored separately from the agent's interpretation and rationale.
- An approval binds to one exact proposal and one exact candidate version. Any semantic change to
  either invalidates the approval and requires a new one; formatting-only or unrelated metadata
  changes do not. Invalidated approvals remain immutable history.
- Application may be performed by a later eligible agent, not necessarily the one that captured the
  approval.
- A rejected correction does not automatically invalidate the underlying finding.

This is the same lifecycle `hold_evidence`/`held_human` and `record-human-decision` implement today
(see [`hold-resolution-design.md`](hold-resolution-design.md) for the proposed synchronous
fast-path). Treat that document and this one as describing the same lifecycle at different levels
of detail — the inline-resolution proposal there is a mechanism for reaching the states defined
here faster when Marco is live in the same run, not a competing model.

## Safe reclaim

This introduces a third action, distinct from the two Part I already ships
(see [`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md)):

- **`recover-lease`** — the same run resumes the same operation after a recoverable lease
  interruption. Unchanged by this design.
- **Safe reclaim (new)** — a different run takes over after lease expiry or explicit termination,
  creating a new linked operation and fencing the old owner. This is the mechanism described below.
- **`abandon-operation`** — reserved for genuinely unsafe or uncertain recovery states, where safe
  reclaim's predicate (below) does not hold. Unchanged by this design.

"No second abandonment step required" means: a safely expired or terminated operation can be
reclaimed directly by a new run, through the safe-reclaim action, without first being formally
abandoned. It does not mean same-run lease recovery and cross-run reclaim collapse into one action
— they stay separate, and `recover-lease` keeps working exactly as it does today.

Reclaim is legal only when committed database state proves all of:

- no consequential command is running, pending, or uncertain;
- no external effect lacks terminal `applied`/`not_applied` settlement;
- no proposal, application, or settlement step is incomplete;
- no projection attempt has an unresolved outcome;
- no live lease or claim is held by another owner.

This must be one mechanically checkable predicate used consistently by service code, `dish-admin`,
and tests — not inspection of a database server's live transaction list.

Safe reclaim may be invoked by an eligible agent when the mechanical predicate passes; it does not
require Marco's approval, because it does not authorize a semantic mutation — it only transfers
workflow ownership. An implementation must not route it through Human Review.

Reclaim creates a new, mechanically linked operation:

- the inactive operation remains immutable for audit;
- the successor restarts only the agent-owned execution portion of the relevant Research,
  Verification, or Planning step. Durable findings, evidence, Marco discussion, recorded decisions,
  and valid proposals remain attached and must not be regenerated merely because ownership changed
  — only unapproved, in-progress agent work may be discarded;
- Marco's existing discussion, exact words, and still-valid approvals remain available where they
  still apply;
- the successor link, previous owner, new owner, reason, and timestamps are recorded;
- atomic fencing rejects any late write from the previous owner.

Formal abandonment and succession stay reserved for genuine recovery risk — pending execution,
unresolved external effects, uncertain outcome, incomplete settlement, or partial mutation.

## Marco override

Dish has no agent-controlled hard blocks. Two override paths exist:

- **Direct admin override** — Marco acts through `dish-admin` directly.
- **Override communicated through an agent** — must require Marco's explicit words. An agent must
  never infer override from urgency, clarification, frustration, or continued discussion.

One explicit override ends repeated challenge on that specific concern. The result:

- preserves the concern (it isn't deleted, just no longer blocking);
- records Marco's exact words;
- identifies the version accepted for use;
- moves the task out of `needs verification`.

An override may be reopened only for materially new evidence or Marco's explicit request.

**Materially new evidence, defined:** evidence is materially new only when it adds previously
unrecorded factual information, supported where applicable by a source, that could reasonably
change the accepted decision — not merely a new agent interpretation, a repeated concern, stronger
wording, or Marco revisiting the same facts again. A new source alone is not materially new evidence
when it only corroborates an already-considered fact.

Required acceptance tests:

- same concern, same evidence → override remains closed;
- a different agent restates the same concern → remains closed;
- a new source supporting an already-known fact → normally remains closed;
- a new factual contradiction affecting the accepted version → may reopen;
- Marco explicitly requests renewed Verification → reopens regardless of evidence;
- reopening records exactly what new evidence triggered it.

## Whole-version rollback

- Whole-version rollback is required, admin-only, and requires Marco's explicit confirmation.
- Restoring an older version creates a new canonical version; it never deletes or rewrites history.
- Each canonical version links to its parent, and retains the exact diff, rationale, and approving
  authority.
- The audit record for a rollback retains: the prior version, the exact applied diff, the approving
  authority, a brief rationale, and the resulting version.

## Operational execution updates (cook logs)

This supersedes the speculative "Tool-mediated cooking and cook logs" direction previously carried
in `future.md` — it's the same idea, now with a decided shape rather than open design questions.

Connected agents need a first-class, append-only action for recording what actually happened during
preparation or cooking:

- ingredient substitutions;
- actual quantities used;
- deviations from the planned method;
- timing, equipment, and environmental observations;
- results, failures, and lessons learned;
- whether Marco wants the canonical Dish revised afterward.

Required guarantees:

- an agent can write an update without direct Asana access (or, post-cutover, without Asana being
  the primary interface at all);
- recording an update does not require opening Research or Verification;
- an update is not automatically a canonical recipe mutation — it's a separate durable record;
- any later promotion of an observation into the canonical Dish goes through the normal Human
  Review proposal/authorization path, not a shortcut;
- updates are attributable, replay-safe, durable, and visible through the primary Dish interface;
- the capability must not require or assume any particular backend — it should work whether Asana
  is authoritative, read-only, or retired.

Connected cooking agents have authority to record observed execution facts directly; these updates
do not require separate Marco approval. An implementation must not treat an actual quantity,
substitution, or deviation as a protected "Decision" requiring the Human Review approval ceremony —
that ceremony is reserved for the later, separate promotion into canonical content described above.

This does not change how cooking agents interact with the guarded workflow otherwise: cooking or
logging a cook must never require the task to be unblocked or past any particular workflow state
first (existing constraint, restated in `future.md`'s cook-log section — kept here as it still
applies).

## Admin surface implications

Whatever backend eventually carries this, `dish-admin` needs to expose:

- direct override out of `needs verification`;
- preview and explicit confirmation of whole-version rollback;
- safe-reclaim eligibility inspection, with reasons when reclaim isn't currently legal;
- display of Marco's exact approval or override evidence (his words, not just an agent's summary).

Existing lease termination and `abandon-operation` stay as-is; safe reclaim (above) is a new third
action for the case where state is already mechanically safe, so it should not need to go through
`abandon-operation` first.

## Deliberately out of scope here

This document intentionally excludes the PostgreSQL schema, migration plan, phased implementation
backlog, and cutover mechanics that were bundled with this content in the source discussion — that
material belongs with [`database-backend.md`](database-backend.md) and
[`postgresql-cutover.md`](postgresql-cutover.md)/[`postgresql-cutover-imp.md`](postgresql-cutover-imp.md)
if and when it's actually being planned, and shouldn't be duplicated or pre-committed here.
