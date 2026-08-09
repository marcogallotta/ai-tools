# Dish workflow and administration design

This is the canonical product/workflow design for Dish. It records the behavior Marco wants agents,
Dish, `dish-admin`, and any later frontend to present, including behavior that is not implemented yet.
Architecture and runtime documents describe current authority and mechanics; when current code differs
from a settled decision here, that is an implementation/documentation gap to reconcile, not permission
to silently reinterpret the product decision.

This document is deliberately storage-agnostic. Before PostgreSQL cutover, Asana remains authoritative
for live task content/placement and SQLite remains authoritative for Dish workflow/control state. After
cutover, PostgreSQL becomes authoritative and Asana is downstream. See
[`database-backend.md`](database-backend.md), [`postgresql-cutover.md`](postgresql-cutover.md), and the
[architecture index](architecture/index.md) for those authority boundaries.

## Decision provenance and change discipline

The major clarifications in this document were closed with Marco in an extended workflow review ending
2026-08-09, after checking the current code, [`runtime-contract.md`](runtime-contract.md),
[`future.md`](future.md), the architecture knowledge base, and the current Honest Planning, Research,
and Verification protocols.

Paragraphs labelled **Marco decision — 2026-08-09** record points Marco explicitly settled in that
review. Future agents must not replace those decisions with a more elaborate or theoretically cleaner
model unless Marco explicitly reopens the point. Where an exact implementation shape was inferred from
Marco's requirement plus an existing architecture invariant, it is labelled **design conclusion**
rather than attributed to Marco verbatim.

As of that review, there are **no outstanding Marco product questions for the current pre-cutover
workflow/admin scope**. Items explicitly marked deferred in this document are future design work, not
hidden blockers that should be re-asked before fixing the current workflow.

## 1. Core workflow principles

- Marco is the final product authority. Agents may warn clearly, preserve evidence, and recommend a
  safer/better route, but substantive agent judgment is not itself execution authority.
- Dish owns legal workflow actions. Agents read the task/current operation and follow the action Dish
  exposes; they do not reconstruct a state machine from protocol prose or invent transitions.
- A tool pass is deterministic conformance, not culinary/research/Verification judgment. Conversely,
  tool or schema failure is a tooling problem, not Evidence or Human Review.
- Findings, evidence, proposals, authorization, application, signoff, and projection/movement are
  distinct facts even when the normal user experience collapses several mechanical steps into one
  action.
- Discussion, urgency, silence, disagreement, frustration, or continued conversation are not formal
  authorization. Ordinary conversation may still resolve ordinary clarification; formal authorization
  is reserved for the governed cases defined below.
- Approval binds an exact proposal/change bundle. Application of that exact bundle remains a separate
  durable action per [ADR-0003](architecture/decisions/0003-approval-and-application-are-separate.md),
  but it does not need to be a separate human or agent ceremony.
- Every consequential continuation is derived from fresh authoritative state. A successful mutation
  must not hand the caller a stale continuation computed before that mutation.

## 2. Task entry, reads, and action discovery

A connected agent starts with the task, not with lease/cycle/request internals.

- `read`/`inspect` should tell the agent the task's legal next workflow action and the exact start kind
  where a start is required.
- With no active operation, discovery and admission share one authority: bare task -> Planning,
  accepted Research-entry task -> Initial Research, `pending-verification` -> Verification, and an
  exact signed resting `ready` task -> Change.
- Post-signoff Change is legal only when the exact current `ready` identity has durable Dish signoff
  lineage. Status text alone never authorizes Change.
- If Dish cannot provide a legal agent continuation, the agent should report the human consequence
  briefly (normally that the task needs Dish admin attention) rather than asking Marco to reason about
  internal operation IDs, leases, cycles, or request journals.
- The admin surface owns the complete operator journey from diagnosis through safe recovery and back
  to an agent-continuable state.

**Marco decision — 2026-08-09:** agent-facing workflow should remain interface-agnostic. `dish-admin`
is today's operator surface; a later UI may replace most shell interaction without changing these
semantics.

## 3. Planning

Planning is the dish-design stage, not recipe construction. The current Honest Planning protocol is
the detailed stage contract.

- A Planning brief may be a compact template or a detailed proposal. It must be coherent enough that
  Research knows the intended dish, purpose, important boundaries/locks, and what uncertainty it must
  resolve.
- Planning does not have to settle exact recipe ratios, sourcing, technique details, nutrition
  arithmetic, or every authenticity question; those belong to Research.
- Planning priors and suggestions are not sacred merely because Planning wrote them. A recorded Lock,
  Exemption, or durable Marco Decision has stronger authority than an ordinary Planning suggestion.
- Planning should front-load a real choice when Marco's judgment is actually needed rather than
  pretending the choice can be settled downstream by agent preference.

## 4. Research

Research turns the Planning proposal/specification into a complete, sourced, executable canonical
recipe and self-reviews that exact candidate before Verification.

- Research owns web/source research, construction, arithmetic, ingredient forms, sourcing, execution,
  nutrition calculation, success criteria, and the complete cooking brief.
- Research may challenge a genuinely incoherent, unsupported, unsafe, or unworkable Planning premise.
  It must not silently optimize away a significant characteristic merely because another route looks
  better.
- Minor/local corrections that preserve the agreed dish are agent-owned. A materially different route
  should be presented to Marco with the issue, recommendation, and consequence before Research commits
  it.
- When two or more genuinely reasonable routes remain and the choice matters to the dish, Research
  talks to Marco rather than silently choosing an alleged optimum. Whether that conversation is
  ordinary clarification, a remembered choice, or formal Human Review depends on the three-level
  human-input model below.
- A missing material fact only Marco can supply is Evidence, not Human Review. Research should state
  the exact missing fact and resume after receipt.
- `Self-verified` means the exact complete candidate has been reviewed end-to-end by its latest
  material editor. It is not meaningful evidence while an agent is still halfway through construction.

**Marco decision — 2026-08-09:** a significant substitution such as changing the defining meat/cut
should not be made silently just because an agent can justify it. A small execution-preserving change
may be agent-owned; a significant route change should be proposed and discussed.

## 5. Verification

Verification is a fresh independent semantic review of the exact Research/self-reviewed candidate.
The current Honest Verification protocol supplies the detailed five-part review; this section fixes
several product semantics that must not be lost in implementation details.

- A verifier may challenge Research. It should not willy-nilly redesign the dish or silently choose a
  materially different route when multiple sensible corrections exist.
- A **Small** correction preserves the settled construction. The verifier may fix it, self-review the
  corrected exact content, re-inspect, and sign in the same run.
- A **Large/material** correction changes identity/route, quantities/ratios/portions/nutrition,
  governed decisions, feasibility, sourcing, safety, halal treatment, or another material property.
  The correcting agent may construct and self-review the new candidate but must not provide the fresh
  independent signoff for its own material edit.
- Tool failures, Action-schema defects, backend errors, and recovery problems are tooling failures;
  they are never converted into dish Evidence or Human Review blockers.

### Nutrition means the consumed served portion

The existing nutrition limits remain hard protocol limits. The estimate used to test them must be an
honest estimate of the **edible food actually served and expected to be consumed**, including stated
sides, not a gross sum of all raw ingredients entering the pot.

- Bones, shells, trim, discarded cooking liquid, and rendered/discarded fat are not consumed merely
  because they appeared in the raw ingredient list.
- Retained sauce, absorbed liquid, edible skin, finishing fat, and other components actually served
  do count.
- When yield/retention is unknowable, use one reasonable defensible estimate with stated assumptions
  when one exists. Do not invent false precision.
- A shaky gross estimate cannot establish a hard-limit violation. Ordinary uncertainty near a limit
  follows the protocol's non-blocking-note rule until there is defensible evidence of an actual
  violation.
- Purchase-price Human Review is different: its thresholds intentionally use the purchased form as
  the protocol states; do not apply edible-yield logic to the purchase-price trigger.

**Marco decision — 2026-08-09:** the thresholds are hard. The failure in the live fat case was not the
existence of a threshold; it was using a poor approximation that did not represent the served edible
portion.

### Repeated Verification passes are a loop detector

After repeated independent passes fail to converge, Dish should stop automatic cycling and surface the
loop for operator attention. That stop is **not** three semantic strikes that suddenly create a Marco
product decision.

- The current `resolved`/Verification-hold behavior is conceptually a loop breaker: release or
  investigate the loop, then run another fresh Verification pass.
- It must not fabricate a substantive Human Review decision or treat the third verifier's concern as
  automatically requiring Marco to settle the dish.
- The operational goal is visibility into recurring small edits, agent oscillation, or tooling
  friction so the root cause can be fixed.

**Marco decision — 2026-08-09:** the three-pass rule exists to detect loops, not to turn a repeated
verification failure into a Human Review judgment.

## 6. Human input: clarification, remembered choice, and governed authorization

Not every useful conversation with Marco is Human Review. Dish needs three levels:

1. **Ordinary clarification or preference.** The agent asks Marco, receives the answer, and continues.
   Example: a preference between two ordinary ingredient routes. No admin ceremony is required.
2. **Meaningful choice worth remembering.** The agent may durably record an attributed decision such
   as "Marco chose X for this dish" so later agents know it was deliberate. This still does not require
   formal authenticated admin authorization merely because future agents should remember it.
3. **Governed exception or consequential authorization.** A nutrition exemption, purchase-price
   exception, reversal of a Lock/Exemption/governed constraint, or another consequential scope that
   requires durable Marco authority uses the formal authenticated Human Review/admin path.

The level follows the consequence, not the conversational wording. If an otherwise ordinary choice
would mutate a governed field or create an exception, it becomes level 3 for that mutation.

**Marco decision — 2026-08-09:** agents are trusted to record ordinary and meaningful conversation
accurately. Do not build formal authentication ceremony around every "Marco said X" record. Formal
Human Review is for governed/consequential authorization, not ordinary questions or corrections.

### Evidence is separate

Evidence is for a material factual input that the agent cannot establish and only Marco can provide.
It uses the same deliberate pause/resume shape as Human Review when the answer must outlive the
current invocation, but it does not create a Marco policy/judgment decision.

### Synchronous conversation is the normal path

When Marco is present in the same conversation and the question can be answered immediately, the
agent should talk first and continue once the required record/authorization exists. A durable hold is
for an actual pause or cross-run handoff, not mandatory ceremony for every synchronous answer.

A deliberate Evidence/Human Review pause must be a completed checkpoint: all agent-owned work that is
safe and meaningful to commit before asking is already committed, and the record states one exact
remaining human dependency. Do not park half-finished private reasoning and expect another agent to
continue it.

### Same-agent continuation after Human Review/Evidence

- If Marco resolves the held question and the candidate itself did not receive a material edit, the
  same live agent may continue from the resulting durable continuation if it remains otherwise
  eligible. Creating a fresh cycle/identity does not by itself mean the conversation must die.
- If resolution requires a material edit, that same agent may apply/build/self-review the authorized
  change, but it becomes a material editor and a fresh independent verifier must sign the resulting
  candidate.
- If the original invocation disappeared, a replacement agent resumes from the deliberate checkpoint;
  it does not inherit uncommitted private reasoning.

**Marco decision — 2026-08-09:** the current restriction that prevents the original live verifier from
continuing after a resolved Human Review is not desired behavior.

## 7. Human Review proposals: approval and mechanical application

Human Review should present the substantive decision, not compare-and-swap plumbing.

Required guarantees:

- Review shows the dish/task, relevant issue, exact proposed consequence/change bundle, recommendation
  where useful, and enough local context for Marco to decide even if he knows nothing about that dish.
- Approval is bound to the exact displayed proposal/candidate and governed changes. A changed/stale
  target fails closed; the approval is not silently broadened.
- Approval and application remain separate durable records/actions as required by ADR-0003.
- **Normal operator UX should not require a second agent merely to run `apply-proposal` for an exact
  already-approved immutable candidate.** Dish should revalidate the current baseline and execute the
  mechanical application itself as the next internal step when safe.
- If mechanical application cannot proceed because the baseline changed or another invariant fails,
  the approval remains durable and Dish reports the blocker; it must not mutate a different bundle.
- A resulting material candidate enters a fresh independent Verification cycle.
- Low-level proposal/application commands may remain available for diagnostics, tests, and exceptional
  recovery even if normal UI/CLI flow collapses them.

**Design conclusion from Marco's 2026-08-09 review plus ADR-0003:** "approval" and "application" must
stay separately auditable, but they need not be separate human/agent interactions. The current
agent-facing `apply-proposal` hop is implementation debt in the normal workflow. Current runtime/cutover
docs that describe later application specifically by a fresh eligible agent should be synchronized
when this normal-path UX is implemented; the durable separation/revalidation invariant remains.

For enumerated governed concepts such as nutrition exemptions, the human-facing command/UI should ask
for the semantic decision (for example add/remove a named exemption) and let Dish compute the exact
before/after representation. Keep raw typed-diff authority available as a low-level escape hatch; do
not make Marco hand-type serialized CAS values for routine review.

## 8. Agent runs, abandonment, and replacement

The operator concept is **an outstanding agent invocation working on a task**, not a lease/cycle graph.

### Outstanding-agent inventory

The admin UI/CLI should list outstanding invocations with enough context to identify them quickly:

- task title plus canonical task identifier/link;
- stage (Planning, Research, Verification, etc.);
- when the invocation started;
- last observable activity;
- whether it is actively working, deliberately waiting on Evidence/Human Review, or otherwise blocked.

The system may report observable facts such as "last activity 12 minutes ago" and the last completed
action. It must not infer "the agent is dead" merely from elapsed time.

**Marco decision — 2026-08-09:** Marco decides when an agent is not coming back.

### `kill` / replace intent

The normal operator interface should support one high-level action to declare a specific invocation
abandoned, plus a bulk form for all outstanding invocations. CLI names such as `dish-admin kill
<TASK_OR_RUN>` and `dish-admin kill-all` are acceptable starting vocabulary; a UI may expose an X or
"replace" action instead.

"Kill" means fence/retire that invocation's Dish authority and prepare the safe continuation. Dish
cannot necessarily terminate an external chat process, so a late write from the retired invocation
must be rejected mechanically.

After Marco chooses replacement, Dish owns the deterministic internal sequence: request/effect
recovery if required, lease/cycle fencing, abandonment/succession where required, and creation of a
claimable continuation. Do not make Marco choose historical lease/cycle/request IDs that Dish already
knows.

Low-level recovery, lease, reclaim, abandonment, and reconciliation commands remain available for
engineering tests and exceptional diagnosis. They are not the normal product workflow.

### What survives an abandoned run

**Marco decision — 2026-08-09:** default conservatively.

- An ordinary agent that dies halfway through Planning/Research/Verification contributes no precious
  semantic state merely because it started or inspected. Discard the incomplete attempt and restart
  from the last committed safe boundary.
- A completed candidate/self-review or other committed stage boundary remains ordinary durable task
  state.
- A deliberate Evidence/Human Review checkpoint is precious and survives; it contains the completed
  work plus the exact outstanding human dependency.
- Do not attempt to make a replacement agent continue a verifier's half-finished private reasoning.

Dish may tell Marco "give this task to a new agent" only after the resulting continuation is actually
claimable by a fresh invocation. Lease release alone is not enough.

The production operator investigation supporting this requirement was captured in the 2026-08-09
`dish-operator-model-report.md` handoff artifact; the repository architectural invariant is captured in
[`architecture/operations-leases-and-fencing.md`](architecture/operations-leases-and-fencing.md).

## 9. Pre-cutover recovery policy

Do not build the richer journal/resume architecture against the temporary Asana-authoritative model.

**Marco decision — 2026-08-09:** wait for PostgreSQL authority cutover rather than implement a rich
intermediate-work journal twice.

Until cutover:

- preserve the existing deliberate Evidence/Human Review checkpoints;
- restart ordinary incomplete agent attempts from the last committed task/workflow boundary;
- keep current safe-reclaim/abandonment/recovery machinery available, but hide it behind high-level
  operator intent where possible;
- fix concrete recovery bugs that block live work, but do not redesign genuinely uncertain external
  effects unless they become a recurring operational problem.

**Marco decision — 2026-08-09:** genuinely uncertain external-effect recovery is deliberately deferred
for now. If it becomes a real problem before cutover, fix the concrete case; otherwise ride the current
mechanism to cutover.

### Post-cutover journal/resumability direction

After PostgreSQL becomes authoritative, build resumability around a real Dish-owned journal/checkpoint
model:

- canonical content advances only at a complete governed commit boundary;
- intermediate agent work/evidence may be journaled without becoming canonical;
- deliberate checkpoints identify exactly what is complete and what dependency remains;
- a later invocation resumes from the last committed checkpoint plus useful journaled evidence;
- accidental half-finished private reasoning is still not promoted to trusted state merely because it
  was emitted;
- version/checkpoint identity prevents resuming against a changed canonical base.

This supersedes the abandoned trusted-connected-session/authority-assignment Part II design in
[`abandoned-run-ownership-design.md`](abandoned-run-ownership-design.md). That Part II remains closed
unless Marco explicitly reopens it.

## 10. `dish-admin` operator experience

`dish-admin` is for Marco, not for another AI. Human outcome comes first; internal mechanics are
secondary detail.

### Inspect any task

`dish-admin inspect <task>` must diagnose **any** known task, including a resting task with no open
operation. A missing open operation is a state fact, not a reason the diagnostic command cannot work.

Normal output should answer:

- what task this is;
- what state it is in;
- what prevents progress, if anything;
- what Marco can do now;
- when relevant, what happens after that choice.

Use `-v`/`--verbose` for deeper operation/cycle/request/effect/history detail instead of inventing a
separate normal diagnostic ceremony.

### Recovery should be intent-first

- If there is one safe deterministic recovery path, present the human outcome and run/offer the whole
  sequence rather than making Marco execute `inspect -> command -> inspect -> command` repeatedly.
- If a real human choice is needed, ask that choice first. After the answer, perform every deterministic
  internal step that follows.
- If the next meaningful choice cannot be known until an earlier step completes, do the earlier step,
  then ask the newly relevant choice.
- For truly abnormal/corrupt state, show enough compact evidence to hand the case to an investigative
  agent; do not dump the entire database by default.

### Review presentation

Assume Marco may know nothing about the dish because review often happens during batch work. A review
item should therefore orient him with task title, relevant excerpt/field, exact issue, proposed options
or recommendation, and decision-relevant consequence. Do not dump the whole recipe/history unless he
asks or the UI can show it unobtrusively with the affected area highlighted.

### Command quality

- Runnable commands must be runnable exactly as shown; do not emit executable-looking placeholders.
- Unknown commands should fail locally with a useful suggestion rather than a misleading service error.
- Task URL parsing should normalize harmless query strings/fragments before extracting the task ID.
- Keep low-level admin commands for testing/diagnosis even after higher-level intents exist; clean them
  up only after real usage proves which can be retired.

## 11. Agent reporting

Agent reports should describe the human consequence first.

- Success wording is not important; concise is fine.
- When asking Marco for a decision, include the dish/task name, the relevant part of the dish, the exact
  issue, sensible options/recommendation, and the consequence where it matters.
- When a tool fails, say whether the dish changed, what remains safe, and what Marco needs to do. Do not
  lead with internal tool jargon unless it helps diagnosis.
- When the task merely needs admin recovery, do not make the agent teach Marco lease/cycle mechanics.

## 12. Post-signoff Change

A resting signed task can enter Change only from exact durable signoff lineage.

- Discovery and `start kind=change` must use the same authority.
- A non-material accepted change may preserve the prior signoff lineage when the canonical classifier
  proves it does not require fresh semantic signoff.
- A material change records the new material edit, clears/invalidates prior exact signoff for the new
  content, self-reviews the new candidate, and opens a fresh independent Verification cycle.
- A status string that merely looks `ready` is not enough.

## 13. Project population, manual Asana lifecycle, and post-cutover completion

A project-level audit/dashboard should compare the configured Asana Cooking corpus with Dish's durable
records and classify differences rather than silently omit tasks.

Useful categories are:

- **Healthy/current** — Dish and Asana are compatible for the current pre-cutover authority model.
- **Expected manual lifecycle difference** — a known operator action changed section/project/due date
  without violating a real Dish invariant.
- **Asana-only / not recognized by Dish** — present in the configured corpus but no usable Dish record.
- **Dish-known but Asana missing/unavailable** — requires investigation, except where the current manual
  Cooking History lifecycle explains the move.
- **Real inconsistency** — content, workflow, or required placement conflicts with an actual invariant,
  not merely a different section.
- **Needs migration/repair** — recognized but cannot participate in the supported workflow/schema.

### Pre-cutover manual lifecycle is legitimate

**Marco decision — 2026-08-09:** while Asana is authoritative, Marco legitimately performs lifecycle
work outside Dish:

- moving tasks between sections such as Planned and Eating while using/cooking them;
- setting due dates for when he intends to cook them;
- moving a cooked task out of Cooking into the separate Cooking History project when he considers that
  task done.

Those differences must not automatically be diagnosed as corruption. `due_on` is a planned-cook-date
signal, not evidence that cooking occurred. Manual section/project movement may still conflict with a
specific active operation invariant, but placement difference by itself is not enough.

### Post-cutover lifecycle belongs to Dish

**Marco decision — 2026-08-09:** once PostgreSQL/Dish is authoritative, lifecycle should stop depending
on manual Asana project movement. That is the live authority mode, not another dark launch. Dish should
own explicit completion/lifecycle state. In particular, `Cooked` should be a governed
completion/archive outcome (exact UI/state naming can be refined), and a separate Cooking History
project should no longer be required as the source of truth.

This aligns with [`database-backend.md`](database-backend.md), which already requires completion to be
separate from workflow phase and permits it only through governed Cooked or Archive transitions.
Asana may remain a downstream UI/projection during transition, but its section/project placement must
not become a peer lifecycle authority again.

The earlier `future.md` proposal to add an Asana-specific Archived section before cutover is therefore
superseded by this post-cutover direction unless Marco explicitly asks for a temporary pre-cutover
archive route.

## 14. Phase/task listings and dashboard behavior

Agent/admin listings should eventually answer the workflow question directly (for example "tasks
whose next legal action is Research" or "pending Verification") rather than proxying through Asana
section placement.

Before cutover this can require expensive per-task Asana reads and should not be overbuilt. After
cutover, task content and workflow state share one PostgreSQL authority, so phase-authoritative
listings and project dashboards should derive from that authority without one remote read per task.

Section listing remains useful as a discovery/display aid while Asana is live; it is not the workflow
state machine.

## 15. Observability and historical log synthesis

Per-task diagnosis and system-wide investigation are separate capabilities.

- `dish-admin inspect [--verbose]` diagnoses one task/current situation.
- The durable SQLite history today, and PostgreSQL history after cutover, should support a separate
  investigative workflow that correlates events across tasks/runs/operations/requests to find recurring
  patterns: no-progress recovery loops, repeated schema validation failures, abandonment concentration,
  never-successful recovery routes, stale continuation bugs, or other systemic friction.
- The valuable output is synthesis over structured history, not merely a large raw SQL dump. Some facts
  can be calculated deterministically; an investigative agent may still be needed to connect patterns
  and explain likely root causes.
- Do not create a second event model solely for analytics; use the durable workflow/audit history as the
  source.

**Marco decision — 2026-08-09:** the Codex-style log investigation is a distinct need from making
`inspect` more verbose.

## 16. Protocol, Dish, and Action-schema agreement

The Honest protocols define stage semantics; Dish enforces deterministic workflow/state invariants;
Action/CLI schemas expose the inputs callers are allowed to send. They must agree at their shared
boundaries.

- An Action/CLI argument with a closed runtime vocabulary must expose that same explicit enum in the
  schema/help. Do not publish a free string while runtime secretly accepts only hidden values.
- Runtime validation errors should return the valid values when that helps the caller correct the
  request.
- `allowed_actions`, admission, and command validation must come from one authority rather than three
  partially overlapping state machines.
- Tool/protocol disagreement fails closed and is reported as a contract defect; the agent must not
  repair around it by hand.
- Changes to workflow semantics must update the Honest protocol(s), Dish implementation, Action/CLI
  schema, runtime docs, and deployed connected-agent instructions coherently where each is affected.

The live `correction`/Human Review route schema mismatch that triggered this rule was a product
contract/deployment bug, not user error. The checked-in source now exposes explicit enums for approve
`correction` and reject `route`; preserve that alignment in generated/deployed schemas and regression
coverage.

## 17. Whole-version rollback and Marco override

These earlier decided capabilities remain part of the workflow design and were not reopened by the
2026-08-09 review.

### Marco override

- Dish has no agent-owned substantive hard block against Marco's explicit decision.
- A governed override is explicit, scoped, durable, and auditable. It preserves the concern rather than
  deleting history.
- Reopen a settled override only for materially new evidence that could change the accepted decision,
  or Marco's explicit request. A new agent restating the same concern is not new evidence.

### Whole-version rollback

- Whole-version rollback is admin-only and requires Marco's explicit confirmation.
- Restoring an older version creates a new canonical version; it never deletes or rewrites history.
- Preserve the prior version, applied inverse/restorative diff, rationale/authority, and resulting
  version.

## 18. Operational execution updates (cook logs)

Connected cooking agents need a first-class append-only action for what actually happened during
preparation/cooking: substitutions, quantities, deviations, timings/equipment, observed results,
failures/lessons, and whether Marco wants the canonical dish revised afterward.

- A cook-log update is durable execution evidence, not automatically a canonical recipe mutation.
- Recording observed execution facts does not require Human Review or reopening Research/Verification.
- Promoting an observation into canonical dish content uses the ordinary Change/materiality/
  authorization rules.
- Cooking/logging must not be blocked merely because the dish is currently stuck in Research or
  Verification.
- After database authority cutover, the execution log and Cooked/archive lifecycle should live in Dish
  authority; Asana is at most a projection.

## 19. Deferred workflow capabilities now owned here

These items previously lived in `future.md`. They remain future/deferred where stated, but workflow
semantics belong in this document so there is only one product source of truth.

### Inline Evidence/Human Review resolution

Desired behavior is already defined by sections 6-7: synchronous conversation should not require an
artificial hold round-trip; an async hold remains available when the dependency must survive the run.
Implementation is preferable after cutover when exact task/cycle updates can be one database
transaction rather than an Asana content-identity round trip.

### Atomic governed decision ergonomics

A genuine Human Review outcome that necessarily changes an Exemption/Lock/other governed field should
be one understandable operator decision. Internally Dish may record decision, exact authorization,
and application as separate durable facts. The UI/CLI should not make Marco manually perform multiple
opaque authorization steps when the complete consequence is known up front.

### Unchanged-content re-Verification

If real use needs a fresh Verification of an unchanged already-signed task, add a guarded admin route
bound to the exact current signed identity and then use ordinary independent Verification. Do not use
manual section movement as authenticated reverify intent.

### Verifier-requested fresh look without correction

A verifier may eventually need an explicit "preserve this exact candidate, carry this concern forward,
and ask a fresh independent verifier" outcome that is neither approval nor a correction/rejection.
This is a real observed need but is not required for cutover. If implemented, carry the concern as
structured successor context rather than forcing the next verifier to rediscover it from free text.

### Active Verification -> Planning redesign

There is no settled agent-legal transition for an active Verification attempt whose whole dish purpose
or structure needs replanning. This is **intentionally deferred, not an outstanding question for the
current cutover/workflow work**. Do not invent the transition. A future design pass must decide its
human authority and how replanning context is durably handed back.

### Idea-dish intake and cross-dish planning

A loosely defined idea tier and a higher-level agent that reasons across multiple dishes remain
speculative. No schema/location/promotion model is settled. Revisit only after the structured
PostgreSQL task/read model makes the actual query needs clear.

## 20. Current implementation mismatches (2026-08-09)

These are known gaps against this document, not unresolved product questions:

- `dish-admin inspect <task>` currently resolves through an open operation and can return "no open
  operation for that task" for a resting task; it must become task-level diagnosis.
- Human Review resolution creates a fresh continuation, but a same-run verifier can still hit
  `actor_fact_conflict`; same-agent continuation without a material edit is desired.
- Normal review approval still advertises a separate connected-agent `apply-proposal`; normal operator
  UX should mechanically apply the exact approved bundle after revalidation while preserving separate
  durable approval/application records.
- There is no first-class outstanding-agent inventory plus high-level kill/kill-all/replace action;
  Marco is still exposed to lease/reclaim/abandonment mechanics.
- Project-wide population reconciliation/audit is not yet a complete operator capability.
- The Honest Verification protocol needs the served-edible-consumed nutrition clarification and should
  describe the repeated-pass stop as a loop detector rather than a substantive Human Review decision.
- Honest Research/Verification already exclude routine clarification from Human Review, but they need
  the explicit three-level remembered-choice distinction and same-agent resume semantics above so a
  useful durable note is not mistaken for formal governed authorization.
- The checked-in Action source now exposes explicit enums for approve `correction` and reject `route`;
  the deployed Action and future schema/runtime changes must stay synchronized so this live mismatch
  does not recur.
- Harmless task-URL query/fragment normalization and local typo suggestions are lower-priority
  operator-quality fixes.

## 21. External research and analogues

These sources informed the comparison with other human-in-the-loop/durable-workflow systems. They are
**supporting research, not Dish authority**; the Marco decisions above control Dish behavior.

- OpenAI Agents SDK Human-in-the-loop: sensitive tool calls can pause for explicit approval/rejection,
  and serialized `RunState` supports later resume:
  <https://openai.github.io/openai-agents-python/human_in_the_loop/>
- LangGraph interrupts: execution can persist/checkpoint state, pause for human input, and resume later;
  approval, review/edit, and input validation are distinct patterns:
  <https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangChain Human-in-the-loop middleware exposes explicit `approve`, `edit`, and `reject` outcomes over
  persisted interrupted state:
  <https://docs.langchain.com/oss/python/langchain/human-in-the-loop>
- AWS Step Functions Redrive preserves successful-step results/history and resumes unsuccessful work
  from the failed frontier rather than pretending completed work never happened:
  <https://docs.aws.amazon.com/step-functions/latest/dg/redrive-executions.html>
- Microsoft Durable Task exposes explicit instance suspend/resume/terminate/restart management with
  durable history/status:
  <https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-instance-management>
- Learning to Defer treats referral to a human as a decision distinct from the model's underlying
  prediction, a useful conceptual analogue for separating escalation from semantic verdict:
  <https://proceedings.mlr.press/v119/mozannar20b.html>
- `git revert` is the useful whole-version rollback analogy: append a restorative history entry rather
  than erasing prior history: <https://git-scm.com/docs/git-revert>

The design lesson is deliberately modest: use explicit decisions, preserve durable state at real
pause/commit boundaries, and resume from trustworthy completed work. Do not import enterprise workflow
machinery merely because another system has it.

## Deliberately out of scope here

This document does not define PostgreSQL physical schema, cutover runbooks, frontend implementation,
or deployment mechanics. Those belong to the backend/cutover/frontend architecture documents. It also
does not authorize implementation by itself; it records the product/workflow target against which
implementation should be reviewed.
