# MCP Planning context (pilot)

Dish's authenticated MCP shell exposes **dish_planning_context** to ChatGPT Projects,
Claude Code, and Codex. One request assembles current Honest Pantry documents and Dish history;
there is no client-specific cuisine-routing table.

## Connected-agent call

Use a fresh real `client.run_id` and one of:

- `scope=broad`: Planning protocol, five discovery documents, the complete cooked inventory
  as compact title/ID/recorded-last-cooked rows, and recent cooked-update metadata. **No cook logs
  are read automatically in broad mode.** Retrieve the full immutable logs for each finalist and
  plausible alias on a focused `focus_dish_ids` reload before recommending.
- `scope=block, target=wok-depth-rice`: protocol, cuisine map, dish classes, exact registry row,
  named block detail and declared parent, full cooked inventory, and cook logs for dishes identified
  by UUID **or distinctive leading canonical-title phrases in the child block**. Matching uses
  normalized whole words, not interior title fragments that might describe an unrelated dish.
  The parent is read for context but its unrelated dishes do not enter child history.
  Unknown/ambiguous block
  identities fail instead of guessing.
- `scope=dish, target=<Dish UUID or exact title>`: protocol, canonical Dish when one exists, the full
  inventory and relevant logs. Multiple exact matches fail; a new title returns no canonical Dish.
- Before recommending finalists, call again with `focus_dish_ids=[...]` covering those dishes and
  plausible aliases (up to 16 per request); this fetches their complete logs. The first call does
  **not** certify all candidate/alias histories merely because the inventory is complete.

The `agent` argument is `gpt`, `claude`, or `codex`. Results include the selected document
texts, a pinned Honest Pantry commit, a compact cooked inventory and only the relevant full cook logs.
The receipt is **multiline**, shown at the top of the first Planning answer and after finalist reload:

```text
Context loaded (block: wok-depth-rice)
Docs: dish-planning-protocol, planning/direction, dish-classes, blocks/wok-depth-rice, blocks/wok-heat (parent)
Cook history: 41 dishes cooked in total
Logs read: Nasi goreng (3), Khao pad (2)
Finalists checked: none yet
```

Names and counts reflect what was *actually retrieved*, not predetermined examples.
`Logs read` lists every Dish whose logs were queried, **including matched block Dishes with
`0 logs`**. On `focus_dish_ids` reload, `Finalists checked` names each checked Dish and its
actual log count (including `0 logs`, which does not establish a previous cook). Imported
Markdown headings and repeated title prefixes are cleaned **for receipt display only**; the
underlying canonical Dish identity and title are not edited. No receipt means the run
did not finish a validated bundle.

All cooked-update and selected cook-log pages are exhausted. Block title matches and their
Dish IDs are exposed in `history.block_title_matches` for audit, but matching a name does not
itself establish that it was cooked. A changed generation during an active
read, missing file, ambiguous identity, or response exceeding **200,000 characters** fails closed:
no receipt is recorded. A pinned source revision is kept as provenance, not a post-read expiry gate.
The `last_cooked` inventory field reflects Dish's current-cooked timestamp; imported timestamps
are not proof of an actual recent cook without an immutable cook log.

## Optional mutation precondition

`DISH_MCP_REQUIRE_PLANNING_CONTEXT=1` makes the **MCP shell** refuse `dish_create` and
`dish_start(kind=planning)` unless the same authenticated owner and exact agent run have already
loaded a successful Planning context. It defaults **off** during qualification; switch on after
the tool is qualified across clients. The receipt store is local SQLite, default
`~/.local/state/dish/planning-context.sqlite3`, overridable by `DISH_MCP_PLANNING_CONTEXT_DB`.
Receipts **do not expire with time or repository HEAD changes**: they record delivery of context,
not model understanding. The original commit SHA and read time remain provenance only.

This guard is at the MCP boundary, not the PostgreSQL command port. **The direct shell CLI and
other non-MCP transports are not fenced by it.** It is not yet a global Dish write-precondition
guarantee. The CLI/API bypass remains a separately documented follow-up. Qualify run ownership,
multi-worker state and create semantics before switching the MCP guard on.

The shell also cannot stop a plain ChatGPT/Claude/Codex opinion given without invoking a tool.
The visible receipt makes this omission observable, not impossible.

## Qualification before production

1. Run `pytest -q dish/tests/test_planning_context.py
   dish/tests/postgresql/test_mcp_app_adapter.py` from the Dish test environment.
2. Test real MCP tool discovery and complete retrieval against **test** Dish and a tracked Honest
   Pantry checkout. Check branch SHA and history cursor exhaustion.
3. Test missing receipts, mismatched users/runs, named-dish reuse and transport retry. Verify a
   prior receipt is still accepted after elapsed time or an unrelated repository commit.
4. Enable the write fence in **test only**; confirm that existing Research/Verification flows
   are not unintentionally blocked. Enable it in production only after qualification.
5. Connect ChatGPT, Claude Code, and Codex to the same tool version. Update their direct first-call
   instructions, then trial the multiline receipts. Do not delete the old routing prose before it
   consistently works.
6. Release through Dish's existing production deployment process and verify the live tool registry.

## Follow-up list (not part of the pilot)

- **Repository-write context guard:** Evaluate requiring a fresh, authenticated context receipt
  before an agent can modify the Honest Pantry checkout or push a proposed repository change.
  Apply this at actual file-write/GitHub credential boundaries across ChatGPT, Claude and Codex,
  not by routing ordinary PR creation through Dish. Identify every bypass (shell, GitHub connector,
  local worktrees), authorization exceptions and what the guard can prove before implementation.
- **Production-wide Dish mutation protection:** Move any required Planning-write precondition into
  the shared authoritative Dish command path before calling it universal; an MCP-only toggle is
  insufficient while CLI/HTTP transports remain accessible.
- **Meaningful-change refresh trigger:** Re-evaluate a run's context after a materially relevant
  change such as a new cook log on a matching Dish or a changed chosen-Dish decision. This is a
  future semantic invalidation rule, **never** an arbitrary time limit or repository-HEAD check.
- **Broad-context size and client acceptance:** A read-only production-data test measured broad
  mode at about 202k characters with 15 recent full cook logs. Removing those automatic full logs
  should reduce the initial bundle to about 144k characters; the hard cap is raised to 200k to
  leave modest growth room. These are estimates until retested on current production data.
  Keep the full cooked inventory and recent-update leads; never silently truncate. ChatGPT's
  acceptance of a 120–200k MCP response still needs a live, cross-client activation test.
- **Cross-client acceptance:** In a real ChatGPT Project, Claude Code and Codex session, check the
  same block and named Dish; confirm the same routed documents, cook-log coverage and visible
  receipt. Do not require a slash command.
- **Behavioral limits:** A receipt proves delivered documents and history, not that the agent
  followed them; ordinary answers without tool calls remain unenforceable by Dish.

The pilot-only test workflow is removed from this PR after passing its targeted checks, so it does
not become a permanent main-branch workflow. No GitHub PR checks or Claude-specific slash command
are introduced here.
