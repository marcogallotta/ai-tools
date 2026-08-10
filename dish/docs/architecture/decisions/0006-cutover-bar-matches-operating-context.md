# ADR: Cutover bar matches actual operating context

Status: Accepted

## Read this when

Read this when deciding whether dark-launch evidence is sufficient to cut over, when scoping what a pre-cutover gap must fix before cutover can proceed, or when comparing this program's pace/rigor against a heavily-used multi-operator production service.

## Scope

How much dark-launch evidence and parity is actually required before cutover, for this specific program: a single-operator system, not a heavily-used live product serving other people.

## Authoritative implementation

Current implementation anchors live in [Dark launch](../dark-launch.md) and the cutover program (`postgresql-cutover.md`). Exact module locations may change without changing this decision.

## Actors, processes, and stores

The relevant actors are the operator (sole user and sole on-call), the legacy Asana-backed system, PostgreSQL, dark-launch capture/shadow-execution/comparison tooling, and the agents (Claude, ChatGPT/codex) that build and operate all of it.

## Authority and data ownership

This ADR does not change authority. Dark launch still does not transfer authority ([0001](0001-dark-launch-does-not-transfer-authority.md)); evidence is still bounded to what protects transfer, recovery, and first admission ([0005](0005-cutover-evidence-is-bounded.md)). This ADR sets how much evidence within that bound is enough, given who actually bears the cost of a missed gap.

## Invariants

- Dark launch's original purpose was to learn whether leaning on AI agents to operate a complex rollout like this is viable at all. That question is answered (yes) and is closed; it is not an ongoing reason to keep extending dark launch.
- The cost of an undiscovered gap surfacing after cutover is bounded: the operator notices and fixes it. This is not a heavily-used live product where a missed edge case affects other people. A program that becomes multi-operator or externally relied-upon must revisit this ADR before continuing to apply it.
- As of 2026-08-10, Marco is actively reconsidering `postgresql-cutover.md` §1.3 ("Asana becomes a projection/interface, not an independent editing authority") toward a full clean break — Asana retired entirely at cutover, not kept as a demoted interface layer. Rationale given: enough cutover infrastructure already exists that a full break looks cleaner and lower-risk than maintaining any ongoing Asana role, given the frontend is now good enough to be the actual interface. This is a live reconsideration, not yet a ratified change to the baseline design — §1.3 still says "projection/interface" until Marco explicitly updates it. If and when it is finalized as a full break, gaps that are purely about correlating PostgreSQL to Asana (identity mapping, historical parity, content-staleness relative to the legacy source) stop needing exhaustive resolution before cutover, because the system they bridge to is being removed rather than kept running alongside PostgreSQL.
- Marco was explicit that this reasoning is context-specific, not a general stance: in a genuinely high-stakes, multi-user product, the right approach would instead be a long dark-launch period with gradual, piecemeal authority migration onto PostgreSQL and Asana retained until each piece is separately proven — the opposite of a fast clean break. The calibration in this ADR applies because this is a single-operator system where a missed gap is cheap to fix, not because fast clean breaks are generally correct.
- Gaps in PostgreSQL's own command/workflow logic — correctness independent of any Asana comparison — are a different category and remain a real precondition for trusting PostgreSQL as sole authority. This ADR narrows the *comparison-completeness* bar; it does not narrow the *does the target's own logic work* bar.
- "We did the work, don't jump early" still holds: this ADR is not license to cut over on a hunch. It changes what counts as sufficient evidence, not whether evidence is required.

## Process and transaction boundaries

Not applicable — this ADR does not change how evidence is collected or transacted, only how much of it is required and which categories of gap block cutover.

## Normal flow

When a dark-launch finding surfaces, classify it: correlation/bridge-only (Asana-identity mapping, historical content parity, capture-schema-version staleness) versus command-logic (PostgreSQL's own execution of a command produces a wrong result independent of any legacy comparison). Correlation/bridge-only gaps are tracked but do not block cutover. Command-logic gaps do.

## Failure, replay, recovery, and concurrency

Not applicable — this ADR concerns evidence sufficiency, not runtime failure handling.

## Change routing

If this program's operating context changes — more than one operator, external users, or a decision to keep Asana running long-term alongside PostgreSQL rather than retiring it at cutover — this ADR must be revisited before continuing to rely on it.

## Proving tests

Not applicable in the usual sense — this ADR does not itself require new tests. It changes how existing dark-launch findings should be triaged: confirm a finding is correlation-only (not a `command_port.py` logic defect) before treating it as non-blocking.

## Current debt and temporary compatibility

As of 2026-08-10, every dark-launch gap found and reviewed to date (identifier-binding resolver gaps on old-format captures, the `create`-command correlation gap, task-content staleness relative to the one-shot importer) has been correlation/bridge-only, not a `command_port.py` logic defect. This should be re-confirmed, not assumed, the next time a new gap class surfaces.

`postgresql-cutover.md` §1.3 still states the pre-existing approved baseline ("Asana becomes a projection/interface, not an independent editing authority"), which has not yet been updated to match the full-clean-break reconsideration described above. Until Marco explicitly updates §1.3, treat it as the authoritative statement of the cutover plan and this ADR's clean-break framing as the leading candidate reconsideration, not settled fact.

## Related documents

- [Dark launch](../dark-launch.md)
- [0001 — Dark launch does not transfer authority](0001-dark-launch-does-not-transfer-authority.md)
- [0005 — Cutover evidence is bounded](0005-cutover-evidence-is-bounded.md)
