# Dish v1a activation runbook

This is the one-cutover checklist for moving from the tool-independent protocols to the guarded
agent-facing `dish` workflow. It does not authorize a live migration by itself.

## Release gate

- Prepare one exact release bundle containing the three tool-aware protocols, both canonical
  manifests, and the wrapper-owned `protocol_release` file.
- Validate that bundle separately and run `tests/test_dish_tool_stage9.py` against those exact bytes.
- Confirm the resolver accepts the committed bundle from the clean protocol worktree and that the
  tool integration suite passes without a live Cooking-task write.
- Confirm the agent-facing protocols contain only `dish` workflow instructions. They must not expose
  the generic task CLI or the Marco-only recovery surface.

## Snapshot-safe corpus migration

Follow `~/honest-pantry/dish-docs-design.md` exactly for the authoritative snapshot-safe migration.
Do not replace or paraphrase that procedure here. Record the source snapshot, release identity,
migration result, unresolved tasks, and rollback point before cutover.

Do not activate a partial corpus. If any managed task cannot be migrated or explicitly dispositioned,
stop with the old authority still intact.

## One cutover

1. Hold protocol-managed note changes for the migration window.
2. Validate the exact release bundle and tool integration suite.
3. Complete and verify the snapshot-backed corpus migration.
4. Switch the governing agent instructions and release pointer together.
5. Verify that all managed tasks and new work resolve to the same active release before reopening
   writes.

Never leave mixed production authority: the tool-independent and tool-aware workflows must not both
be presented as current. If verification fails, restore the recorded snapshot and previous governing
release before reopening writes.

During implementation and fixture validation, perform no live Cooking-task write. Live migration and
activation require Marco's separate execution approval under the authoritative migration procedure.
