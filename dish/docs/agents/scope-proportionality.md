# Scope proportionality and authoring trajectory

Use this shared procedure to keep an accepted implementation slice bounded before, during, and
after authoring. It creates no intent authority, approval gate, planning artifact, task-splitting
system, or Review verdict.

## One intent chain

The exact active Marco Intent Baseline and applicable accepted specification are the sole authority
for **what** the work must accomplish. `CURRENT OBJECTIVE`, `REQUIRED NOW`, `RESERVED / FUTURE`, and
`NON-GOALS` are disposable working projections of that authority. They never reconstruct, edit, or
replace it. If a projection conflicts with the governing baseline, repair the projection.

If material intent authority cannot be established, do not infer it from historical task prose,
accumulated notes, nearby designs, or implementer memory. Route the affected decision through the
existing intent/design authority. Ask Marco only when the remaining material fact is genuinely
human-only, using the epistemic-sufficiency procedure.

The same exact baseline/specification identity governs three distinct checks:

1. **Before authoring:** the dispatch owner verifies that the implementation handoff is a faithful
   projection of the governing authority.
2. **During authoring:** Implementation compares the actual trajectory with the baseline-bound
   current slice and expected solution envelope.
3. **After authoring:** semantic Code Review independently checks the delivered candidate against
   that same governing authority. Passing the authoring check is not a Review verdict.

## Proportional current-slice framing

For non-trivial or amplification-prone work, frame the cheapest useful view of:

- `CURRENT OBJECTIVE` — the exact outcome being implemented now;
- `REQUIRED NOW` — behavior, constraints, compatibility, and evidence required by this slice;
- `RESERVED / FUTURE` — valid context that is not current implementation scope;
- `NON-GOALS` — adjacent work that must not be absorbed; and
- `EXPECTED SOLUTION SHAPE` — the likely established mechanism, surfaces, pieces, and material
  non-complexity expectations.

The first four fields project **what** from the governing baseline. The solution envelope calibrates
**how**; it cannot redefine outcome, scope, constraints, quantifiers, or non-goals. For a trivial
established change with negligible amplification risk, no persisted four-section plan is required.
A few internal lines—or no separate artifact—are sufficient when the same baseline plainly governs.

## During-authoring trajectory brake

Before continuing material semantic expansion, reconcile any material structural departure from the
current slice or expected solution shape. Strong signals include:

- a new service, scheduler, queue, database, persistent registry, authority/identity layer, or broad
  lifecycle not justified by the governing intent;
- unexpected subsystem entry caused by generalizing the solution;
- conversion of reserved/future context into current functionality;
- a materially new operator workflow or maintenance burden; or
- replacement of a bounded extension with a generalized framework.

Large diffs, many files, generated/test volume, elapsed time, or commit count are tripwires only.
They never decide proportionality. Conversely, a tiny high-consequence change retains every safety,
security, migration, Review, Integration, and operational control required by its governing invariant.

Choose one outcome:

- **SHRINK AND CONTINUE** — remove the unnecessary expansion, restate the smaller envelope when
  useful, and continue without Marco.
- **JUSTIFIED EXPANSION** — when concrete evidence proves a materially broader mechanism necessary,
  stop only that expansion and route the consequential design/scope choice through existing
  design/Marco authority before building it.
- **SEPARATE DEPENDENCY / OWNER** — preserve adjacent or independently owned work on its canonical
  owner and continue the bounded slice when safe.

When an attempt was stopped or rejected for scope amplification, a second attempt must state a
materially changed/smaller solution envelope or new evidence that genuinely justifies the former
shape before material authoring resumes. Repeating the same over-broad interpretation is recurrence,
not recovery.

Load historical/future evidence just in time when it can constrain the current decision. Historical
design archaeology is not automatically active implementation scope. Do not build a scheduler,
database, service, intent registry, approval state, automatic splitter, or other control plane to
enforce this procedure.
