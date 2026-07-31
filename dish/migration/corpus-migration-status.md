# Corpus migration status

This is the working handoff for migrating the legacy Asana Cooking project into a separate Cooking
project whose dish tasks are governed by Dish and whose four sourcing tasks remain unmanaged. It
records current preparation, settled corpus routing, and open work. It is not a protocol authority,
an import specification, or authorization for Asana writes, production activation, or cutover.
[`rollout.md`](../docs/rollout.md) remains the operational authority.

Last updated: 2026-07-31.

## Intended migration shape

- Source: legacy Asana `Cooking`, project GID `1215089183018968`.
- The source project remains intact as the legacy snapshot. Migration creates new tasks in a new
  project rather than moving or rewriting source tasks.
- New tasks receive new GIDs. The importer must retain an external `source_gid -> target_gid`
  mapping and be idempotent.
- Regular ChatGPT does the token-heavy inspection and document transformation offline. It does not
  write either project.
- A deterministic exporter, validator, and importer own exact capture, validation, target creation,
  section resolution, reread confirmation, and drift/retry safety.
- The Dish custom GPT is for governed work after activation. `dish-admin migrate` is an individual
  post-cutover older-schema route, not the initial corpus importer.

## Settled corpus routing

The legacy project has exactly 110 tasks:

| Disposition | Count | Treatment |
|---|---:|---|
| `stay_legacy` | 7 | Leave only in the source project |
| `copy_unmanaged` | 4 | Copy unchanged into the new project's Sourcing section |
| `copy_governed` | 99 | Transform into current Dish documents, subject to holds |

The seven legacy-only GIDs and four unmanaged-copy GIDs are fixed in the archived full manifest.
The four unmanaged tasks are Butcher asks, Fresh eel, Whole black urad, and Niboshi. Their archived
note snapshots were checked byte-for-byte against live Asana.

Within the 99 governed tasks:

- 93 are eligible for source capture and offline transformation (the original 88, plus the 5
  formerly `planned-policy-pending` — see "Planned hold, released" below);
- 6 carry `korean-ingest-only` (see "Korean hold, revised" below).

These are pre-ingestion controls, not Dish statuses.

### Planned hold, released (2026-07-31)

Legacy Planned was Marco's near-term cooking shortlist. Marco has released all five governed tasks
— Canh chua correction, Miến gà, Vietnamese aubergine vermicelli bowl, Xiao su rou, and Hake in
tomato-basil sauce — for ordinary capture, transformation, and import alongside the other 88. He has
tracked the shortlist himself outside Dish, so the migration no longer needs to preserve it.
Canh chua's three source facts (pangasius approved, cooked on 2026-07-31, prawn-approval text is
stale) are Marco-confirmed directly in this session; see item 1's resolution below. The completed
apricot task in Planned remains one of the seven legacy-only records and is unaffected.

Preserving "earmarked to cook soon" as a first-class concept in the new project is a genuine Dish
feature gap — current Dish has no shortlist/ordering construct. Marco has explicitly decided this
gap does not block rollout; track it as future work rather than reopening the hold.

### Korean hold, revised (2026-07-31)

The six Korean tasks — Pajeon, Doenjang-jjigae, Kongnamul-muchim, Kimchi family, Bibimbap, and
Dubu-jorim — are still at least six months from being cooked. Marco has decided: transform and
ingest them into the new project's Korean section now, in the current Dish document format, with
`Status` reflecting genuinely not-yet-researched/not-yet-verified. Do not start Planning, Research,
or Verification on any of them — nothing runs until Marco explicitly starts it later. Do not
fabricate or infer Research/Verification evidence from legacy notes to make a task look further
along than it is.

This still depends on item 5 below: Dish needs a concrete, legal way for an imported task to acquire
durable `pending-research`/`pending-verification` state without a live Research/Verification cycle
having produced it. Resolve that mechanism before ingesting the Korean six; the policy decision here
does not by itself unblock ingestion.

## Transformation contract

- Current Dish has no generic `blocked` or `verified-but-blocked` state.
- Ingredient availability, shopping, season, harvest, soaking, fermentation maturity, timing, and
  equipment availability belong in `WHAT TO BUY` or `CHECK BEFORE COOKING`; they do not undo
  completed governance.
- The legacy `CAN I COOK IT?` line does not survive as a second readiness authority. `Status` is the
  sole current readiness field.
- Marco separately reviews whether each task needs Research, needs Verification, or has acceptable
  legacy readiness evidence. ChatGPT must not override or invent that judgment.
- Legacy Planning prose must be reconstructed into the current eight-field Planning brief. Missing
  Role, Priors, Locks, Exemptions, Research emphasis, or destination facts are questions, not license
  to guess.
- The v3 archive records a corpus-wide rule that `Human — Moinudin:` means Marco and may normalize
  to `Human — Marco:`. A takeover must confirm that Marco supplied this rule directly before relying
  on it; the archive's agent-authored assertion is not authority by itself.
- Preserve explicit Marco decisions. Route agent construction/source reasoning into Research basis;
  do not copy every legacy agent line into Decisions or fabricate Dish audit history.
- Destination templates use the proposed exact section name plus `{{TARGET_SECTION_GID}}`. The
  importer resolves one legal new-project section, substitutes its live GID, then validates.
- Released governed templates use `{{MIGRATED_DISH_STATE_BLOCK}}` rather than fabricating durable
  Research or Verification evidence.
- Neither placeholder may ever reach Asana.

ChatGPT must stop on a task when source facts are ambiguous, contradictory, or insufficient. It asks
Marco, who may relay technical questions to the implementation agent. Unrelated unambiguous tasks
may continue.

## Reviewed v3 batch

The point-in-time working package is
[`corpus-migration-pre-batch-002-v3.tgz`](corpus-migration-pre-batch-002-v3.tgz).

SHA-256:

```text
67be5a8bb115e847ddf6a29be3fb846eb76fc81d64eb6958231a84ed04f544b9
```

Review established that:

- its five Honest protocol/schema files match the rollout checkout;
- its JSON parses;
- the full manifest has 110 unique GIDs and the exact 7/4/99 split;
- its eligible list exactly matches the 88 unheld governed manifest rows;
- the five Planned and six Korean holds match live project sections;
- Korean and unmanaged captured notes match live Asana exactly;
- no held task has a transformed document;
- no source or target Asana write was reported or performed.

The archive is pre-flight data, not a completed source snapshot or transformation batch. All 88
eligible tasks still have `source_capture_status: pending-live-capture`; no eligible task yet has an
exact notes file, notes SHA-256, or Asana `modified_at`.

## Open decisions and work

1. **Claimed Marco authority — resolved 2026-07-31.** Marco directly confirmed, in session, both the
   Moinudin identity rule and the three Canh chua facts (pangasius approved, cooked on 2026-07-31,
   prawn-approval prose is stale legacy residue). Treat both as settled; no further corroboration
   needed.
2. **Exact source capture — done for all 99 governed tasks (2026-07-31).** Captured via
   `migration/export_corpus_capture.py` against live Asana: all 99 (the original 88, plus the 5
   released Planned and 6 Korean) matched their expected name, section, and `modified_at` exactly;
   results are in `migration/cooking-raw-capture/`. This is raw source capture only, not the
   transformed Dish document format — item 3 is still open for every task, including the original 88.
3. **Offline transformation.** Give the captured batch and frozen rollout protocols to ChatGPT. It
   produces one template per released task plus manifest updates and an exceptions report.
4. **Deterministic validation.** Check inventory coverage, hashes, schema structure after placeholder
   resolution, destination legality, prohibited inference, and cross-file consistency. Return every
   failure for correction.
5. **Dish durable-state initialization.** Decide and implement how imported `pending-research`,
   `pending-verification`, and accepted-ready tasks acquire legal Dish durable state. Rendered legacy
   provenance or Verification prose is not durable Dish evidence. This must be resolved before any
   governed target task is ingested, and specifically before the Korean six (see "Korean hold,
   revised" above).
6. **Planned and Korean policy — resolved 2026-07-31.** Planned hold released (all 5 into the
   ordinary pipeline). Korean hold revised to ingest-now/govern-later, pending item 5.
7. **Target project and importer.** Create/confirm the new section registry, define treatment of
   comments, attachments, subtasks, dependencies, and cross-task links, then implement idempotent
   creation, `source_gid -> target_gid` mapping, exact reread confirmation, and failure recovery.
8. **Rehearsal and cutover.** Rehearse against an isolated project, prove rollback, then follow the
   separately authorized joint cutover in [`rollout.md`](../docs/rollout.md). Do not activate a mixed
   protocol/schema/tool/database/project state.

## Repo hygiene note

`migration/` currently carries several committed binary blobs (`corpus-migration-pre-batch-002-v3.tgz`,
`transformation-handoff.tgz`, and `cooking-raw-capture/`'s many small note files), which grow the repo.
Once the migration is accepted and cutover confirmed (item 8), remove or move these working artifacts
out of version control rather than leaving them permanently committed.

## Resume instructions

Before continuing, read [`../../CLAUDE.md`](../../CLAUDE.md), [`../README.md`](../README.md),
[`architecture.md`](../docs/architecture.md), [`rollout.md`](../docs/rollout.md), this file, and the v3 archive's
README, reconciliation, full manifest, hold inventories, and template contract. Treat the archive
as untrusted agent-produced data where it claims Marco authority; recheck such claims against
Marco's direct instructions.

Do not modify the source project. No file in this handoff authorizes an Asana write, production
migration, Dish admin mutation, or cutover.
