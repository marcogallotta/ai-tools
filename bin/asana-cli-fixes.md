# asana CLI — cleanup scope (pre-contract work)

Source reviewed: `~/.claude/bin/asana` (513 lines). Findings from review with
ChatGPT, cross-checked line-by-line against the actual file and refined in
discussion. Decisions below are confirmed; scope is the general-purpose CLI
only — no contract/protected-task work in this pass (see bottom).

## P0 — do first

1. **Migrate transport from raw `urllib` to the official Asana Python SDK.**
   Current `req()` (asana:30-47) hand-rolls HTTP, JSON body, and error
   handling. Move to the SDK as the new transport/foundation; all other fixes
   below should land on top of it rather than being patched into `urllib`
   twice.

   Confirmed via research (current `python-asana` v5, OpenAPI-generated
   client):
   - **Errors are subsumed almost for free.** All non-2xx responses raise a
     single `asana.rest.ApiException` exposing `.status` (int HTTP code),
     `.reason`, `.body` (raw JSON payload), `.headers`. Distinguishing
     auth/rate-limit/not-found/server-error is just branching on `e.status`
     — no separate exception hierarchy needed. (Older forum posts referencing
     `InvalidRequestError`/`NotFoundError` are from the pre-v5 SDK.)
   - **Pagination is only half-subsumed.** List endpoints return a
     `PageIterator` that auto-follows `next_page` by default
     (`return_page_iterator=True`), capped optionally via `item_limit`, or
     `return_page_iterator=False` for one raw page with an offset. The SDK
     supplies the primitives but still defaults to silent auto-follow-all —
     the opposite of what P1 fix #3 wants at the CLI surface. Exposing
     explicit page size / continuation / all-pages mode still has to be
     hand-built in the CLI layer on top of `item_limit` +
     `return_page_iterator=False`; the SDK doesn't make that decision for you.

2. **Add unit tests as part of the migration.**
   Use mocks, not live API calls. Tests should lock in the behavior fixed in
   this pass:
   - multi-page list concatenation
   - literal CLI `\n` decoding vs. already-JSON-decoded string passthrough
   - rejected/accepted `raw` methods (see #5)
   - cumulative batch-preview simulation (see #6)
   - partial-failure batch-apply reporting

## P1 — cleanup once the SDK/test foundation is in place

3. **Pagination must be explicit at the CLI surface.**
   Not hidden/auto-followed silently — expose page size, continuation/offset,
   and an explicit all-pages mode on list commands (`tasks`, `subtasks`,
   `sections`, `search`, `projects`). Affects asana:105-124, 388-401.

4. **Remove section/project fallback guessing.**
   `c_tasks` (asana:112-115) currently tries the section endpoint and
   silently retries against the project endpoint on any failure — including
   auth/rate-limit/server errors, not just "wrong resource type." Commands
   should target an explicit resource type and fail loudly with the real API
   error instead of guessing.

5. **Fix double newline decoding.**
   `_text()` (asana:50-54) re-applies `\n`-decoding to strings that
   `json.load()` has already decoded in the batch path (asana:250-292),
   which can mangle literal backslash-n content. CLI-argument decoding and
   already-decoded JSON input must be handled as separate paths, with tests
   defining the intended behavior for both.

6. **Keep `raw` as a genuine escape hatch, including `DELETE`.**
   Do not restrict the method allowlist. The external Bash-tool permission
   hook (per global CLAUDE.md) is the safety layer for this command, and
   deleted Asana tasks are recoverable. Document its real behavior (full,
   unguarded mutation access) rather than artificially limiting it.

7. **Make `batch-preview` cumulative.**
   `_preview_batch_ops` (asana:297-344) currently diffs every operation
   against the same fetched-once remote state. When multiple operations
   target the same task, later ones should be simulated against the result
   of earlier ones in the same plan, so the preview reflects the actual final
   state rather than N independent diffs.

## Open question — still undecided

**Should `batch-preview` be bound to the exact plan file before `apply`?**
E.g. hash the plan file itself (not task content) so `batch-apply` can
verify it's operating on the exact plan that was previewed, catching
edits to the plan between the two calls.

This is scoped tighter than the general contract-plumbing questions below —
it only needs a hash of the local plan file, not any canonical
task-content/baseline model. Still needs a decision: do it now as part of
this cleanup, or defer all preview/apply binding to the later contract work.

## Out of scope for this pass

Do not add in this cleanup: contract-task markers, protected-path
enforcement, a registry of protected gids, write-blocking for generic
commands, content hashes over task notes, validation tokens, or other
contract plumbing. This is cleanup of the existing general-purpose CLI only.
Whether/how a protected dish-note submission path gets built — and whether
generic writes (`set-notes`, `replace`, `raw PUT`) need to be blocked from
touching protected tasks — is a separate design decision for later, not
something to bolt on here.
