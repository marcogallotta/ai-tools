# Dish Task Contract Tool — Future / v2+ Ideas

**Purpose:** Holds everything about the dish-task contract tool that is NOT part of the v1 design —
the v1b enforcement flip, v2 candidate features, and ideas considered and rejected outright. Split
out of `dish-task-contract-tool.md` so that doc can stay focused on exactly what v1 needs to exist
and work.

**Status:** Not authorized for implementation. Nothing here is scheduled; items move into the v1 doc
(or a future v1.x/v2 doc) only when Marco explicitly decides to build them.

## Versioning plan

The tool is built and rolled out in stages, scoped to what the evidence in `dish-docs-design.md`,
`dish-incident-log.md`, and `dish-review-log.md` actually requires. Nothing beyond v1a/v1b is built
until real usage data justifies it.

For the later split contract, the approved checked-in `dish-planning.md`, `dish-research.md`, and
`dish-verification.md` are repository-maintained governing sources, not generated or synced from
Asana. One human-readable `contract_release` version identifies their exact checked-in set together
with the manifest/schema. The shared `git-commit` wrapper owns the version file: agents and humans
do not edit it; the wrapper advances and includes it atomically whenever a file in the defined
contractual set changes, rejects direct edits or an unversioned contractual commit, and ignores
unrelated commits. Git history supplies the exact-content binding, so the release value need not be
a combined hash. `tool_version` remains separate; compatible tool-only fixes change only that
version, while schema or semantic compatibility changes require both versions to advance. The
cross-repository enforcement mechanism is still an implementation question, not authorization to
build it.

**v1a — build and soft-launch.** The full guarded path (`prepare` / `approve` / `reject` / `submit`
/ `contract-admin recover`) is implemented, tested, and usable end-to-end against live tasks — it
performs real Asana writes through the guarded, token-protected path. What v1a does *not* do is make
this path mandatory: the existing generic Asana CLI still works for managed tasks, and its
managed-task check runs in advisory/log-only mode (see Contract-managed task registry, Logging and
observability). This proves the structural validator against the contract's manifest, the lock-based
submission state machine, verifier correction/return flow, and uncertain-outcome recovery under real
conditions. It also produces the usage data needed to decide v1b's timing and v2's scope.

**v1b — enforce.** Once v1a's validator and lock workflow have run clean against real usage, the
generic CLI's managed-task check is flipped from advisory to blocking. No new mechanism is added at
this stage — v1b is a configuration flip on v1a's own logged evidence, not new code.

**v2 — add once v1a data justifies it.**

- The two-failed-pass stop rule (`dish-task-contract.md` lines 206-209) — real contract text, but no
  incident shows it failing in practice; v1a's rejection-rate logging is exactly the evidence needed
  to decide whether to build it. Cheap to add once needed (a counter and a gate).
- The small-change (`small`/Local) carelessness speed bump — Marco's standing concern (see Out of
  scope (all versions) in `dish-task-contract-tool.md`): an honest agent carelessly mis-declaring a
  material change as `small`, not a malicious one gaming the system. Designing its trigger condition
  needs data on what real `small`-declared diffs actually touch and how large they are — but v1a
  does not collect that (see Deferred below: diff-summary fields are dropped from v1a entirely, and
  only rule-level pass/fail logging is kept). So this item isn't just waiting on v1a's existing logs
  to accumulate; it needs diff-summary computation added first, as its own v1.x step, before the
  trigger condition can be designed from real data.
- Bounded direct-dependency surfacing (see Direct dependencies) — already scoped in the change plan
  as advisory and non-blocking; natural to add once the core write path is proven.
- Structured task-title construction (incident 22): take `--dish-name` plus repeatable `--blocker`
  and `--optional` values and render the canonical marker prefix rather than asking agents to hand-
  format it. This guarantees title syntax, not semantic completeness; verification still catches
  omitted real blockers or trivial marker dumping. Repeatable options avoid ambiguous commas. When
  this managed title path ships, title joins notes inside the controlled task surface; section and
  completion state remain ordinary lifecycle metadata outside it.
- Exact-content binding and detection of edits made outside the controlled workflow. V1 deliberately
  trusts its lock and does neither. Consider hashes or another mechanism only if real usage shows
  that the accepted external-edit risk or an approval-to-submit handoff is causing problems.
- Split-planning lock handling: use one lock type. The Decisions/process record preserves who signed
  off; a Marco-signed lock may deserve more evidence to challenge than an agent-set lock, but that
  remains judgment. Do not encode separate lock classes or evidence thresholds in the tool. Any
  proposed lock change after planning handoff goes to Human Review.
- Scripted migration for later contract releases: perform deterministic structural transformations,
  remove obsolete fields, stamp the new `contract_release`, and stop on content requiring judgment
  rather than inventing it. This follows the split's initial snapshot-backed, agent-led local
  migration, where literal template validation returns every structural failure for correction
  before scripted upload; it does not replace that first rollout or claim semantic equivalence.
- Token/submission replacement as a distinct action from `contract-admin recover` — only worth
  building if recovery proves insufficient in real use.
- Replacing the manual ChatGPT copy/paste relay (`dish-task-contract-tool.md`'s ChatGPT workflow
  section) with a custom GPT Action calling a live endpoint directly, cutting out the local
  human/agent relay step. Marco already has the underlying pattern proven on a laptop-hosted custom
  GPT for another purpose (Action endpoint, hosting, schema registration), so this isn't a new infra
  bet — but it's punted to v2 by Marco's explicit call, not incident evidence: it changes the trust
  model (ChatGPT calling directly into a live endpoint against real tasks, vs. today's model where
  nothing executes until a local agent runs `contract prepare`/`submit`) and what `Self-verified:`
  actually asserts (ChatGPT's own claim in its output vs. something the Action layer stamps on its
  behalf). Decide the trust/semantics question before building. The same endpoint may later mediate
  reads: give a cook the complete signed above-divider brief, a shopper the buying/quantity view,
  and a verifier the complete candidate and provenance. Named-section reads are useful for bounded
  work, but must not replace a complete cook view where quantities, timing, fallbacks, and warnings
  depend on one another.

**Dropped, not deferred.** These were considered and rejected outright, not postponed:

- `--confirm-independent-review` as a separate required flag, and any dedicated "self-verification
  collision" detection alongside it. The opposite-family requirement on `approve` already makes
  `editor_agent == verifier_agent` structurally unreachable — routing rejects it before any
  collision check could run — so a comparison built to catch that case is dead code, not a
  protection. The real residual risk (one session dishonestly declaring `claude` for editing and
  `gpt` for verification) is exactly the "trusted, not authenticated" limit already stated in Scope,
  and no mechanical check catches it; claiming one does would be worse than naming the gap.
- A cached, authoritative `managed_tasks` table. Management is always resolved live (see
  Contract-managed task registry); a cache that isn't authoritative isn't worth maintaining.
- A distinct adversarial self-review mechanism. The review log is explicit that this "was an
  assistant recommendation and was not approved in the enforcement handoff" and creates no
  implementation requirement. Stays out unless Marco separately approves it.
- Cryptographic identity authentication, recursive dependency audits, and a multi-user/remote trust
  service (see Out of scope) — no new reason to revisit these.

## Deferred: write-safety and observability machinery with no evidenced incident (v2)

Checked against `dish-incident-log.md` and `dish-review-log.md` in full: none of the following is a
response to any recorded incident. v1a ships the simpler behaviour noted under each; build the
fuller mechanism only if v1a's own logging actually shows a problem.

- **`write_count` escalation (silent write / `--final`-gated write / hard block /
  `contract-admin reset`).** No incident involves an accidental duplicate write, a repeat-write
  failure, or anything else this guards against. v1a: `submit` performs exactly one write per
  submission and logs it; no confirmation round-trip, no reset mechanism.
- **`contract-admin recover`'s full crashed/uncertain-outcome table.** No incident involves a
  crashed process or an ambiguous Asana API outcome. v1a: an uncertain `submit` outcome is logged
  and left for Marco to check directly in Asana; the deterministic recovery table is only worth
  building once a real crash/ambiguous-outcome case actually occurs.
- **Diff-summary fields (`characters_added`, `characters_removed`, `lines_changed`,
  `headings_touched`) computed at `prepare`.** No incident needed this granularity. What the
  evidence actually supports is rule-level pass/fail logging (which validation rule failed, how
  often) — that stays in v1a; the per-character diffing does not.
- **The fuller periodic-summary query list** (small-change diff characterization, `--final`/reset
  frequency). These feed decisions (the v2 small-change speed bump's trigger, the write-limit's
  level) that don't exist yet because the machinery they'd tune isn't being built in v1a either.
  v1a's logging keeps only what's evidenced: validation-failure rate by rule, and advisory-bypass
  count.

## Direct dependencies (v2)

Deferred; not built in v1a/v1b. Dependency surfacing is advisory and must not block submission
status in any version. A later scanner may surface only bounded direct candidates: exact task-GID
references; explicit Asana links; exact task-name references; clearly named planning documents. It
must not recursively audit dependencies or decide semantic impact.

## Candidate deterministic checks flagged during dish-verification.md checklist drafting (v2)

Raised while drafting `dish-verification.md`'s compact checklist in honest-pantry
(`dish-docs-design.md`, "Research/verification split and file rename"). V1 deliberately avoids
field-value grammar. This section records only later candidates whose automation would first require
a narrow input syntax; semantic and culinary truth remain verifier work.

- **Task-title validation (incident 22).** Use the structured construction specified in the v2 list
  above; do not attempt brittle blocker inference from free-form notes.
- **Three-value nutrition enforcement (incidents 5 and 23).** If automated in V2, parse calories,
  protein, and fat per complete served portion and require 750-1,000 kcal, over 40 g protein, and
  under 40 g fat unless an explicit Planning lock or Marco approval covers the departure. Do not add
  carbohydrate parsing, 4/4/9 reconciliation, or warning tolerances. Exact field and exception-tag
  syntax is deliberately deferred until this automation is chosen for implementation.
- **`WHAT TO BUY` / `QUANTITIES` reconciliation (incident 21).** v1a only checks that `WHAT TO BUY`
  is present, not whether each purchase amount reconciles with recipe use, live stock, usable yield
  or trim, and package/minimum purchase quantity. A later check needs a defined per-ingredient
  syntax that distinguishes those values and permits an explicit reason for a difference; literal
  numeric equality is not the invariant.

None is scoped into v1a's implementation plan. Title construction and every field-value grammar are
explicitly V2; nutrition timing and purchase-reconciliation syntax remain for later implementation
review.

## Open decision: small-change carelessness (v2)

Marco's concern is an honest agent carelessly mis-declaring a material change as `small`, not a
malicious one gaming the system. The fix should be a deterministic speed bump, not independent
verification for every `small` change — but its trigger condition, what it actually requires of the
agent, whether it's a hard block or a warning, and where it lives are all undecided. v1a's logging
of real `small`-declared diffs is the intended input for designing this (see Versioning plan,
Logging and observability).
