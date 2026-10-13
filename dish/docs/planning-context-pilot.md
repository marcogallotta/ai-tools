# MCP Planning context (pilot)

Dish's authenticated MCP shell exposes **dish_planning_context** to ChatGPT Projects,
Claude Code, and Codex. One request assembles current Honest Pantry documents and Dish history;
there is no client-specific cuisine-routing table.

## Connected-agent call

Use a fresh real `client.run_id` and one of:

- `scope=broad`: the Planning protocol, five discovery documents, a recent cooked pass, the full
  cooked inventory, and its available immutable cook logs.
- `scope=block, target=wok-depth-rice`: protocol, cuisine map, dish classes, the exact registry row,
  the named block detail and declared parent, both history passes, and logs. Unknown/ambiguous block
  names fail rather than guessing.
- `scope=dish, target=<Dish UUID or exact title>`: protocol, canonical Dish when one exists, the full
  cooked inventory for matching, and relevant logged outcomes. A title with multiple exact matches
  fails. A new title returns no canonical Dish rather than inventing one.

The `agent` argument is `gpt`, `claude`, or `codex`. The result includes all selected document
texts and the history material, a pinned Honest Pantry commit, and a one-line `receipt`.
**Show that line in the Planning answer**, e.g. "Context: block wok-depth-rice, 5 docs, 12 cook logs
(Honest <sha>)". No receipt means the run did not finish a validated bundle.

All cooked-update and cook-log pages are exhausted. A changed generation, stale repository, missing
file, ambiguous identity, or response over the configured budget fails closed: no receipt is recorded.

## Optional mutation precondition

`DISH_MCP_REQUIRE_PLANNING_CONTEXT=1` makes the **MCP shell** refuse `dish_create` and
`dish_start(kind=planning)` unless that authenticated owner and exact client run have a fresh context
receipt against the current Honest checkout. Defaults to **off** during pilot qualification. The
receipt store is local SQLite, default `~/.local/state/dish/planning-context.sqlite3`, overridable
by `DISH_MCP_PLANNING_CONTEXT_DB`. This records context delivery, **not** model understanding.
Receipts expire after 90 minutes and are invalidated when Honest Pantry HEAD changes.

This guard is at the MCP boundary, not the PostgreSQL command port. **The direct shell CLI and
other non-MCP transports are not fenced by it.** It is not yet a global Dish write-precondition
guarantee. Do not enable the guard in production until CLI/API bypass, run ownership, multi-worker
state, receipt expiry, and create semantics have been validated or moved into the shared authority.

The shell also cannot stop a plain ChatGPT/Claude/Codex opinion given without invoking a tool.
The visible receipt makes this omission observable, not impossible.

## Qualification before production

1. Run `pytest -q dish/tests/test_planning_context.py
   dish/tests/postgresql/test_mcp_app_adapter.py` from the Dish test environment.
2. Test real MCP tool discovery and complete retrieval against **test** Dish and a tracked Honest
   Pantry checkout. Check branch SHA and history cursor exhaustion.
3. Test stale/missing receipts, mismatched users/runs, named-dish reuse, and transport retry.
4. Enable the write fence in **test only**; confirm that existing Research/Verification/CLI flows
   are not unintentionally blocked.
5. Connect ChatGPT, Claude Code, and Codex to the same tool version. Update their direct first-call
   instructions, then trial the one-line receipts. Do not delete the old routing prose before it
   consistently works.
6. Release through Dish's existing production deployment process and verify the live tool registry.

No GitHub PR checks or Claude-specific slash command are introduced here.
