# Review V2 design lineage

## Scope

Review V2 is the sole exact design-generation and design-snapshot lineage authority for design-bearing Asana tasks. This contract does not create a database, service, scheduler, second lifecycle controller, or alternate design history.

The mechanical implementation is `scripts/review_design_lineage.py`. It models the immutable generation record, append-only generation events, projection contradiction checks, exact recovery, cumulative-drift baseline lookup, successor validation, and the R9 bounded challenge/reviewer-replacement rules.

## Exact generation identity

Each design generation is an immutable `dish-design-generation:v1` record containing:

- `task_gid`;
- `generation_id`;
- `predecessor_generation_id` when there is a predecessor;
- exactly one of an inline `canonical_snapshot` or a durable `canonical_snapshot_ref`;
- `canonical_sha256` over the exact canonical bytes;
- `relevant_repo_baseline` when material;
- `created_at`;
- `created_by`.

The exact identity is `(task_gid, generation_id, canonical_sha256, relevant_repo_baseline)`. A stored inline snapshot must match its digest at construction. A referenced snapshot must match its digest when recovered.

Generation bytes never contain mutable approval/dispatch state.

## Append-only lifecycle events

`dish-design-generation-event:v1` events bind to the exact generation identity. Supported event types are:

`CREATED | MARCO_APPROVED | DISPATCHED | REOPENED | SUPERSEDED | CANCELLED`.

State is reconstructed from the immutable generation plus the durable ordered event stream. Invalid transitions, duplicate events, and identity mismatches are contradictions; they do not rewrite or replace the design bytes. `SUPERSEDED` and `CANCELLED` are terminal.

Marco approval therefore binds only the exact generation identity that received a valid `MARCO_APPROVED` event. Dispatch similarly freezes only the exact generation that received a valid `DISPATCHED` event.

## Successor and post-dispatch semantics

A material semantic design change creates a successor generation in the same Review V2 lineage. A successor references its predecessor; a competing second successor is surfaced as a lineage fork rather than accepted as an alternate history.

A dispatched generation cannot silently acquire a successor while remaining dispatched. Reopen or supersede semantics must first be present in its event history. Consumers then move their projection to the Review V2 successor identity; they do not mint their own replacement generation.

## Consumer boundary

Asana V1, Lifecycle V3/resolver, and mutation-control/service infrastructure are consumers of Review V2 identity only:

- design-bearing note recovery uses the Review V2 generation snapshot;
- non-design material note recovery may use a generic notes-preimage mechanism and receives no Review V2 identity;
- lifecycle or service persistence transports the exact Review V2 identity tuple unchanged;
- a current-generation task pointer is projection only;
- another subsystem's different design bytes are a contradiction, never competing authority.

The Review V2 generation remains authoritative when a projection, cache, service copy, or other subsystem disagrees with it.

## Cumulative drift

Cumulative-drift comparison is anchored to the nearest current/ancestor Review V2 generation with a valid `MARCO_APPROVED` event. It is not anchored to the immediately prior edit merely because that edit is newer.

Materiality classification remains a Review/Marco authority question. The lineage implementation supplies the exact baseline identity and bytes; it does not downgrade materiality or create human approval by consensus.

## R9 author/reviewer continuity and bounded challenge

Author and reviewer continuity are not required. A blocker/challenge cycle is keyed by the exact candidate generation identity, blocker ID, and durable evidence-set digest. Replacing the author or reviewer does not reset the one-challenge budget for that substantive blocker/evidence set.

A replacement independent reviewer may issue `UPHOLDS`, `NARROWS`, `REFRAMES`, or `WITHDRAWS` against the exact inherited challenge. Any agent included in cumulative material authorship of that candidate is not independent and cannot clear that candidate.

A materially changed candidate or new material evidence creates a different key and may justify a new bounded cycle. Routine engineering disagreement remains an independent Review matter; a challenge is evidence, not self-clearance.

## Mandatory regression coverage

`ci/tests/test_review_design_lineage.py` proves the R10 boundaries:

1. Asana design recovery reuses the Review V2 exact snapshot;
2. a parallel snapshot mismatch is surfaced while Review V2 bytes remain authoritative;
3. non-design note preimages remain outside design lineage;
4. Lifecycle consumption uses the exact Review V2 tuple without minting a generation;
5. service/mutation persistence preserves that identity unchanged;
6. successor generations stay in the same lineage and projections move to the successor;
7. author/reviewer replacement preserves challenge history and independence rules.

Additional tests cover invalid event sequences, projection contradictions, post-dispatch successor safety, and cumulative-drift baseline selection.
