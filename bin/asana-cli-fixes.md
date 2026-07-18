# asana CLI — cleanup implementation plan

Scope: general-purpose CLI (`~/ai-tools/bin/asana` — `~/.claude/bin/asana`
is a symlink to this file) only — no contract/protected-task work in this
pass (see bottom).

## Rollout

Ship as staged commits, not one combined PR: Step 0 → P0 (#1 then #2) → P1
(#3, then #4/#5/#6). Each stage should land and be verifiable on its own.

## Step 0 — precondition, before any P0 work — DONE

**Remove `batch-preview` entirely.** Do this first, before P0 item #1, not
as P1 cleanup — its removal is a precondition for the rest of the pass, not
just a nice-to-have alongside it, since P0 #2's test list and the migration
itself should not carry forward tests/behavior for a feature being deleted.

Not part of the batch workflow the global CLAUDE.md mandates (build plan →
chat table as the review surface → immediately invoke `batch-apply` in the
same turn). No consumer depends on it — `~/honest-pantry/cooking-master-reference.md`,
the only other place outside global CLAUDE.md that references this tool,
does not mention `batch-preview`.

Remove `c_batch_preview`, `_preview_batch_ops`, and the helpers that exist
only to support it (`_task_snapshot`, `_section_label`, `_format_value`,
`_clip`, `_note_diff` — asana:170-344), plus the `HELP` entry and the
`batch-preview` line in the "batch plan shape" example. None of these
helpers are used by `batch-apply` or anything else in the file.

## P0 — do first

1. **Migrate transport from raw `urllib` to the official Asana Python SDK.**
   Current `req()` (asana:30-47) hand-rolls HTTP, JSON body, and error
   handling. Move to the SDK as the new transport/foundation; all other
   fixes below should land on top of it rather than being patched into
   `urllib` twice.

   Use `python-asana`, latest v5.x, pinned to the current latest release at
   implementation time.

   - **Errors:** all non-2xx responses raise a single `asana.rest.ApiException`
     exposing `.status` (int HTTP code), `.reason`, `.body` (raw JSON
     payload), `.headers`. Distinguish auth/rate-limit/not-found/server-error
     by branching on `e.status` — no separate exception hierarchy needed.
   - **Retries:** the SDK retries `429`/`500`/`502`/`503`/`504` responses
     automatically (up to 5 attempts, backoff factor 2), and for `429`
     specifically honors Asana's `Retry-After` header
     (`respect_retry_after_header=True` by default). Keep the SDK's default
     retry behavior — no custom retry/backoff code in the CLI layer.
   - **Pagination:** list endpoints return a `PageIterator` that auto-follows
     `next_page` by default (`return_page_iterator=True`), capped optionally
     via `item_limit`, or `return_page_iterator=False` for one raw page with
     an offset. The SDK supplies the primitives but defaults to silent
     auto-follow-all — the opposite of what P1 #3 wants at the CLI surface.
     Explicit page size / continuation / all-pages mode has to be hand-built
     in the CLI layer on top of `item_limit` + `return_page_iterator=False`.

2. **Add unit tests as part of the migration.**
   Use `pytest`, with `unittest.mock` (stdlib, no new dependency) — no live
   API calls, mock at the SDK client boundary (patch the `asana.*Api`
   methods, not `urllib`/`requests`). New `tests/` directory, one module per
   concern:

   **`tests/test_transport.py`** — SDK client wiring and error mapping
   - client is constructed once with the configured token; the same client
     instance is reused across commands in a single invocation, not
     reconstructed per call
   - `ApiException` with `.status == 401` surfaces as an auth error message,
     not a raw traceback
   - `.status == 404` surfaces as a "not found" message naming the resource
     gid/type
   - `.status == 429` surfaces as a rate-limit message (assert on message
     content, not just exit code)
   - `.status >= 500` surfaces as a server-error message once the SDK's own
     retries (see P0 #1) are exhausted — not silently swallowed
   - any `ApiException` sets a non-zero exit code

   **`tests/test_pagination.py`** — explicit paging at the CLI surface
   - a single call always fetches exactly one page (mock call count == 1),
     never auto-follows to further pages
   - when more results exist beyond the current page, the CLI prints/returns
     a continuation cursor; when it's the last page, no cursor is returned
   - `--cursor <token>` passed in is forwarded verbatim to the next SDK
     call's continuation param — round-trip test, not just "doesn't crash"
   - `--status` defaults to `incomplete`; `--status complete`/`--status both`
     change the filter sent to the SDK accordingly
   - page size sent to the SDK is always 100 regardless of result-set size
     (asserted via the mock call args), confirming no silent smaller default
   - applies to each of `tasks`, `subtasks`, `sections`, `search`, `projects`
     — parametrize over the five commands rather than writing five near-
     duplicate tests

   **`tests/test_decode.py`** — CLI-arg vs. JSON-decoded string paths
   - a CLI arg containing the literal two characters `\n` is decoded to a
     real newline (existing `_text()` behavior, preserved)
   - a batch-file JSON string value containing an actual newline character
     (already decoded by `json.load()`) is passed through unchanged — must
     NOT be re-decoded (this is the bug: currently a literal backslash-n
     *inside* JSON-decoded content gets mangled into a newline)
   - a batch-file JSON string value containing the literal two characters
     `\n` (i.e. the user wanted a literal backslash-n, escaped as `\\n` in
     the JSON source) round-trips unchanged
   - regression case using the exact input from the bug that motivated this
     fix, if one is on record; otherwise construct the minimal
     backslash-n-inside-JSON case above

   **`tests/test_raw.py`** — `raw` escape hatch (see P1 #6)
   - `raw GET/POST/PUT/DELETE` are all accepted (no method allowlist
     rejection) and each dispatches to the corresponding SDK/HTTP call
   - no method is silently rejected or requires a confirmation flag beyond
     what the Bash-tool permission hook already provides

   **`tests/test_batch_apply.py`** — stop-on-error reporting
   - a batch of N operations where operation *k* raises `ApiException`:
     operations before *k* that succeeded are reported as succeeded,
     operation *k* is reported as failed with its real API error, and the
     batch stops there — operations after *k* do not run. This is the only
     behavior; it is not a configurable batch-file field.
   - summary output accounts for every operation up to and including the
     failure (no operation silently dropped from the report)
   - exit code is non-zero whenever any operation fails

## P1 — cleanup once the SDK/test foundation is in place

3. **Pagination must be explicit at the CLI surface, one page per call.**
   Not hidden/auto-followed silently. Each call to a list command
   (`tasks`, `subtasks`, `sections`, `search`, `projects`) returns exactly
   one page — fixed at 100 (the Asana API max, internal constant, not a
   flag) — plus a cursor if more results exist. There is no all-pages/
   auto-fetch-all mode; the caller (agent) loops via `--cursor` itself. This
   bounds each call's cost/size and keeps fetching everything an explicit,
   stoppable, per-page decision rather than hidden behavior — same
   principle as `raw`'s safety model (bound the blast radius at the
   interface, don't rely on built-in limits). Affects asana:105-124,
   388-401.

   **All list commands** (`tasks`, `subtasks`, `sections`, `search`,
   `projects`) get:
   - `--cursor <token>` — continuation token from the previous call's
     output; omit for the first page. No offset — Asana's API is
     cursor-based.

   **`tasks` and `subtasks` only** additionally get:
   - `--status {incomplete,complete,both}` — default `incomplete` (matches
     today's default). Replaces the old `--all` boolean. Not applicable to
     `sections`, `search`, or `projects`, which have no completion concept.

   **`tasks` semantics change: `--all` is gone.**
   `~/honest-pantry/CLAUDE.md` documents and relies on current behavior —
   "`asana tasks <project_gid> --all` returns every task across all sections
   in one call" and "bare command (no `--all`) is incomplete-only by
   default." Both of those are gone: there is no single-call all-tasks mode
   anymore, and the flag is `--status`, not `--all`. That file must be
   updated in the same pass to describe cursor-based one-page-at-a-time
   fetching.

4. **Remove section/project fallback guessing.**
   `c_tasks` (asana:112-115) currently tries the section endpoint and
   silently retries against the project endpoint on any failure — including
   auth/rate-limit/server errors, not just "wrong resource type." Commands
   should target an explicit resource type and fail loudly with the real API
   error instead of guessing.

   New syntax, matching the existing `asana search project|task <query>`
   convention: `asana tasks section <gid>` / `asana tasks project <gid>`.
   The bare-gid, type-guessing form (`asana tasks <gid>`) is removed.

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

   Dispatch `raw` through the SDK's low-level `ApiClient.call_api()` (method,
   path, body all passed through as given) rather than keeping a second,
   separate direct-HTTP transport alongside the SDK. This is the one place
   in the CLI where an arbitrary method/path/body must reach the API
   unmediated by a named SDK method.

   Current `req()` (asana:37) wraps any body in `{"data": ...}` before
   sending. `raw` must keep doing this wrap itself after the migration —
   `~/plant-monitoring/CLAUDE.md:26-27` documents this exact behavior
   ("`asana raw PUT` wraps the body in `{"data": ...}` itself; pass only the
   inner object"). If the migration changes this (e.g. `call_api()` expects
   the caller to pass the full envelope), that file must be updated in the
   same pass.

## Resolved — declined

**Should `batch-preview` be bound to the exact plan file before `apply`?**
(E.g. hash the plan file itself so `batch-apply` can verify it's operating
on the exact plan that was previewed.)

Declined. The global CLAUDE.md's mandated batch workflow doesn't put
`batch-preview` in the critical path — it has the agent build the plan, show
a Markdown table in chat as the review surface, then immediately invoke
`batch-apply` in the same turn. There's no separate preview-then-apply
window for a plan to drift in. Binding `apply` to a hash of something that
isn't actually being reviewed would add friction without protecting a real
review step. If a mandatory preview gate is ever added to the workflow
itself, revisit this then.

Now doubly moot: Step 0 removes `batch-preview` from the CLI entirely.

## Docs to update in this pass

Every consumer of this CLI outside `~/ai-tools/bin/asana` itself (consumers
invoke it via the `~/.claude/bin/asana` symlink) that this pass could
break, and what to do about each:

- **`~/honest-pantry/CLAUDE.md`** — documents `tasks --all` single-call and
  incomplete-only-by-default semantics (P1 #3). `--all` is gone; must be
  rewritten to describe `--status`/`--cursor` one-page-at-a-time fetching.
- **`~/plant-monitoring/CLAUDE.md`** — documents the `raw PUT`
  `{"data": ...}` body-wrap gotcha (P1 #6). Update only if the SDK
  migration changes this wrap; otherwise leave as-is.
- **`~/honest-pantry/cooking-master-reference.md`** — checked; only uses
  `asana set-notes`, which this pass does not touch. No update needed.
- **In-CLI `HELP` text** (asana:413-446) — update in the same commit as
  each change it describes: the `batch-preview` line and its batch-plan-
  shape example line (Step 0), and the `tasks`/`subtasks`/`sections`/
  `search`/`projects` usage lines (P1 #3, #4) to show the new syntax and
  flags.

## Out of scope for this pass

Do not add in this cleanup: contract-task markers, protected-path
enforcement, a registry of protected gids, write-blocking for generic
commands, content hashes over task notes, validation tokens, or other
contract plumbing. This is cleanup of the existing general-purpose CLI only.
Whether/how a protected dish-note submission path gets built — and whether
generic writes (`set-notes`, `replace`, `raw PUT`) need to be blocked from
touching protected tasks — is a separate design decision for later, not
something to bolt on here.
