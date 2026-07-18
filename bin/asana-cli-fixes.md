# asana CLI — cleanup scope (pre-contract work)

Source reviewed: `~/.claude/bin/asana` (513 lines). Findings from review with
ChatGPT, cross-checked line-by-line against the actual file and refined in
discussion. Decisions below are confirmed; scope is the general-purpose CLI
only — no contract/protected-task work in this pass (see bottom).

## Step 0 — precondition, before any P0 work

**Remove `batch-preview` entirely.** Do this first, before P0 item #1, not
as P1 cleanup — its removal is a precondition for the rest of the pass, not
just a nice-to-have cleanup alongside it, since P0 #2's test list and the
migration itself should not carry forward tests/behavior for a feature
being deleted.

It isn't in the batch workflow the global CLAUDE.md actually mandates
(build plan → chat table as the review surface → immediately invoke
`batch-apply` in the same turn) and its printed diff output has not
actually been read in practice. Checked the only other place outside global
CLAUDE.md that references this tool (`~/honest-pantry/cooking-master-reference.md`)
and confirmed it does not mention `batch-preview` either — no consumer
depends on it. Remove `c_batch_preview`, `_preview_batch_ops`, and the
helpers that exist only to support it (`_task_snapshot`, `_section_label`,
`_format_value`, `_clip`, `_note_diff` — asana:170-344), plus the `HELP`
entry and the `batch-preview` line in the "batch plan shape" example.
Confirmed none of these helpers are used by `batch-apply` or anything else
in the file.

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
   - rejected/accepted `raw` methods (see #6)
   - partial-failure batch-apply reporting

## P1 — cleanup once the SDK/test foundation is in place

3. **Pagination must be explicit at the CLI surface.**
   Not hidden/auto-followed silently — expose page size, continuation/offset,
   and an explicit all-pages mode on list commands (`tasks`, `subtasks`,
   `sections`, `search`, `projects`). Affects asana:105-124, 388-401.

   **Downstream consumer to update if `tasks --all` semantics change:**
   `~/honest-pantry/CLAUDE.md` documents and relies on current behavior —
   "`asana tasks <project_gid> --all` returns every task across all sections
   in one call" and "bare command (no `--all`) is incomplete-only by default."
   If this fix changes those semantics (e.g. `--all` no longer means
   single-call-every-task, or default behavior changes), that file must be
   updated in the same pass, not left stale.

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

## Resolved — declined

**Should `batch-preview` be bound to the exact plan file before `apply`?**
(E.g. hash the plan file itself so `batch-apply` can verify it's operating
on the exact plan that was previewed.)

Declined. The global CLAUDE.md's mandated batch workflow doesn't put
`batch-preview` in the critical path at all — it has the agent build the
plan, show a Markdown table in chat as the review surface, then immediately
invoke `batch-apply` in the same turn. There's no separate preview-then-apply
window for a plan to drift in. Separately, in practice the printed
`batch-preview` diff output has not actually been read before applying, so
binding `apply` to a hash of something that isn't being reviewed would add
friction without protecting a real review step. If a mandatory preview gate
is ever added to the workflow itself, revisit this then.

Now doubly moot: Step 0 removes `batch-preview` from the CLI entirely.

## Out of scope for this pass

Do not add in this cleanup: contract-task markers, protected-path
enforcement, a registry of protected gids, write-blocking for generic
commands, content hashes over task notes, validation tokens, or other
contract plumbing. This is cleanup of the existing general-purpose CLI only.
Whether/how a protected dish-note submission path gets built — and whether
generic writes (`set-notes`, `replace`, `raw PUT`) need to be blocked from
touching protected tasks — is a separate design decision for later, not
something to bolt on here.
