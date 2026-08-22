# Structured task-field lifecycle

This is the shared lifecycle contract for the `Code Area`, `Agent owner`, `Version`, and
`Has Headline` fields in governed V2 Asana projects. Apply it only after the applicable project-mode
contract authorizes the mutation. These fields project current durable authority; they never create
scope, ownership, design, intent, readiness, or mutation authority.

## Code Area

- Blank is truthful for raw or unassessed intake.
- Set it during semantic triage when the actual repository or runtime work surface is known. Use
  multiple values only when the current task materially crosses bounded areas.
- Use `Cross-cutting / Unknown` only for genuinely cross-cutting or unresolved work, never as the
  default.
- Refine it when material scope moves to a different work surface. References to consumers or
  dependencies do not change it.
- It is routing and parallelism context, not conflict proof, ownership, or mutation authority.

## Agent owner

- Blank is truthful until one canonical specialist or sub-role actually adopts or is assigned the
  task.
- Set it on real adoption and update it immediately on a real ownership transfer. A reviewer,
  dependency, or related consumer does not become owner merely by participating.
- Subsumed and terminal source tasks must not remain active dispatch owners solely because a
  historical field value remains.
- The field names the current orchestration owner. The standing role contract, not the field,
  defines that owner's authority.

## Version

- Blank is truthful until this task has an exact durable version, revision, or generation identity.
- Set it to the exact current governing identity and update it on a valid successor. Preserve prior
  generations in immutable chronology; the field projects only the current one.
- Never infer it from a parent program, repository release, or contextual mention such as
  `Review V3` or `Integration V1`.
- The field never proves readiness or Review acceptance.

## Has Headline

The four field states are distinct:

- blank — unassessed or legacy; no conclusion has been recorded;
- `No` — assessed and no separate headline or Intent Baseline applies;
- `Yes - unapproved` — an exact candidate headline exists durably, without Marco approval;
- `Yes - approved` — durable evidence preserves the exact headline shown to Marco and his explicit
  approval.

`Yes - approved` is sticky. Agents may not materially edit, paraphrase, weaken, reinterpret, or
silently supersede the approved words. Only a new explicit durable Marco decision can replace
them. If notes or design drift from those words, classify a reconciliation or design defect; the
durable approved intent wins. Authenticated account metadata, an agent summary, or the field value
itself cannot establish approval or upgrade a candidate.

There is no blanket headline gate. Trivial or raw work may not need a headline, Needs Processing
items may stay blank until triage, and an unrelated safe action on a legacy task is not blocked only
because `Has Headline` is blank.

## Reconciliation checkpoints

Reconcile applicable fields at the smallest natural checkpoint: semantic triage; actual adoption
or transfer; a durable design generation or material scope successor; before declaring Needs
Agentic Review or Ready; before dispatch when the exact owner/version/headline governs the
candidate; and subsumption, terminalization, or reopen. Do not create a lifecycle state or ceremony
only to update metadata.

Backfill active legacy tasks opportunistically from current authoritative evidence. Do not perform
full historical archaeology for cleanliness; blank remains the truthful legacy/unassessed value
until reconciliation occurs. When the correct projection is unclear, use the owning project-mode
contract's `RECONCILIATION_REQUIRED` path rather than guessing.

Priority remains governed by the applicable V2 priority contract and is outside this field
lifecycle.
