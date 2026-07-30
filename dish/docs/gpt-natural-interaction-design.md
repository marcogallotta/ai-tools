# GPT Action natural interaction: design

Status: three tiers, ordered by urgency, not by document position. Nothing here currently blocks
rollout.

- **v1 — soon after rollout, not blocking.** The state-driven Custom GPT instructions, closing the
  create-vs-existing decision gap below (real duplicate-creation risk from loose phrasing like
  "create the mapo tofu dish"). Worth doing soon, but rollout does not depend on it.
  - The tool-side `task_url` extraction originally bundled into v1 is **deferred and currently
    unnecessary**: in practice, agents already resolve Asana task URLs correctly without dedicated
    `dish`-side parsing. The design below is kept as reference; revisit only if real misparsing
    shows up.
- **v2 — not blocking, no near-term timeframe.** `create`'s collision check. Worth having
  eventually, independent of any real evidence of duplicates actually occurring, but not something
  to expect soon after rollout.
- **v3 — deferred, draft design only.** `dish_find` and the `TaskWorkflowSnapshot` authority
  generalization. Not implementation or rollout authorization; tracked as a future candidate via
  `future.md`, which points here for detail. Implement only if v1/v2's known limitations cause real
  recurring friction.

Current behavior remains defined by [`architecture.md`](architecture.md) and
[`runtime-contract.md`](runtime-contract.md); the Custom GPT payload remains
`~/honest-pantry-dish-rollout/dish-custom-gpt-instructions.md` until an approved change lands there.

## Problem

Marco wants to address a dish by natural language — "create that dish," "research this," "verify
that task" — without hitting exact trigger words or having to already hold a `task_gid`. Three
distinct gaps cause the current friction:

1. **Phrasing does not map cleanly to an Action.** `dish-custom-gpt-instructions.md` hands the GPT a
   fixed Action call sequence per stage. It does not tell the agent to derive its next call from the
   tool's own reported state. "Create" collides with the literal `dish_create` Action, which the
   protocol reserves for "only when no task exists yet" — an agent following the words literally can
   call it against a dish that already has a task, producing a duplicate.
1. **No dish-name-to-`task_gid` resolution exists.** Every Action requires a `task_gid` (or, for
   `create`, produces a fresh one). A freshly started GPT session has no way to turn "the mapo tofu
   dish" into the right task without Marco supplying the identifier himself.
1. **The action authority itself only covers two task conditions.** `runtime-contract.md` and
   `workflow_policy.legal_actions` only document/derive legal next actions for a task with an active
   operation, or one just produced by `create`. A resting task — created but never Planning-started,
   sitting between a finished stage and its handoff, on an old schema version, or otherwise outside
   those two conditions — has no documented guarantee that the authority (and therefore `read`) can
   tell the agent what to do next.

Gap 1 is cheap to close (v1's instructions rewrite), just not urgent enough to block rollout. Gap 2
had a cheap partial fix planned (v1's `task_url` resolution), now deferred as unnecessary — see
status above — leaving only the expensive full fix (v3, deferred). Gap 3 is expensive and entirely
deferred to v3 — v1 and v2 both accept that some resting-task states still require asking Marco.

## v1 — soon after rollout, not blocking

No new Action, no new dependency; this is an instructions-only change. The `task_url` resolution
item below was originally scoped as a small, contained backend addition alongside it, but is
deferred (see status above) and kept here only as reference design.

1. **State-driven Custom GPT instructions, with one required exception for `create`.** Rewrite
   `dish-custom-gpt-instructions.md` so that for an existing task (identity already known, from a
   URL or a supplied `task_gid`), the agent calls `read` and chooses its next Action from what it
   reports (`allowed_actions`, `data.required_start_kind`, `data.required_admin_action`) instead of
   a memorized per-stage sequence. **After a successful `create`, the agent follows `create`'s own
   response directly instead of immediately calling `read`.** This exception is required, not
   optional, specifically because v1 excludes the authority generalization: `create` today returns
   `allowed_actions: ["start"]`/`required_start_kind: planning` itself (hardcoded in the command
   handler), but a `read` of that same brand-new bare task finds no open operation and currently
   returns no useful `allowed_actions` at all. An instruction that says "always call `read`" would
   make the agent create successfully, immediately lose its own continuation, and have to ask Marco
   what to do next — the opposite of the goal. When `create` instead reports a collision (v2), the
   agent does call `read` on the returned `task_gid`, since that task isn't new and its state is
   genuinely unknown. `allowed_actions` can legally contain more than one option once an operation
   is open (e.g. `approve` and `reject` both showing during Verification); which to actually take
   remains the governing stage protocol's and the agent's substantive call, not something `read`
   decides. Once v3 generalizes the authority and makes `create` project the same snapshot `read`
   uses, this exception can be removed.

1. **`task_url` resolved by `dish` itself, not parsed by the GPT — deferred, not currently planned;
   see status above.** The implemented `dish-admin expire-lease` target parser is a separate
   operator-only surface, not this deferred agent feature. It deliberately accepts only
   `/0/<project_gid>/<task_gid>` and
   `/1/<workspace_gid>/project/<project_gid>/task/<task_gid>` and rejects the optional old `/f`
   suffix described below. Do not infer that `read` or `start` now accepts task URLs, or reuse the
   narrower expiry grammar as the future agent-surface contract. Deliberately not GPT-side text parsing: extracting the right ID out of a real
   Asana URL is exactly the kind of thing that should be guaranteed-correct code, not an LLM
   applying a grammar from prose and being merely *usually* right — the same principle already
   governing `dish_find`'s deterministic exact tier elsewhere in this design. `read` and `start`
   each accept an optional `task_url` alongside the existing `task_gid`, strictly mutually exclusive
   with it. `create` does not accept `task_url` at all: it has no existing `task_gid` to resolve
   against, and the create-versus-existing decision tree below already routes any known URL or GID
   to `read`, never to `create`. `dish` resolves it deterministically, on both the Action surface
   and the CLI — `dish read`/`dish start` gain a `--task-url` flag alongside the existing positional
   `task_gid`, so Claude and Codex get the same resolution the Action gets, not only the GPT:

   - scheme must be `https`; hostname `app.asana.com`, compared case-insensitively;
   - username/password URL components are rejected; any explicit, non-default port is rejected;
   - accepted path forms and which segment is specifically the task GID — the older form
     `/0/<project_gid>/<task_gid>`, optionally followed by exactly `/f` and nothing else (no
     trailing slash, no other suffix), takes the *last* numeric path segment before that suffix,
     never the first; the newer form `/1/<workspace_gid>/project/<project_gid>/task/<task_gid>`
     takes the segment immediately following the literal `task/`, with no trailing slash or other
     suffix accepted; any other trailing segment is rejected rather than accepted as an unspecified
     "view suffix";
   - percent-encoded path separators or numeric segments are rejected, not decoded into a second
     interpretation;
   - every extracted GID-shaped path segment (workspace, project, and task) must pass the existing
     `dish_service.identifiers.require_asana_gid` validator — numeric only, no leading zero, within
     the supported Asana GID range — not merely match a digit pattern; the resolver still returns
     only the validated task GID, but validating every GID-shaped segment keeps accepted URL forms
     structurally genuine rather than accepting a shape that later fails deeper in the stack;
   - a nonempty query string or fragment is rejected outright, not silently ignored or persisted —
     `dish` never scans them for numbers, so there's no reason to accept or journal characters it
     never looks at;
   - a URL that isn't a recognized Asana task-link form is rejected with a clear `INVALID_ARGUMENT`,
     not silently scanned for the first number found;
   - both `task_gid` and `task_url` supplied → rejected as a conflicting argument, even when they'd
     resolve to the same task — accepting two redundant address forms whenever they happen to agree
     would contradict the "strictly mutually exclusive" contract stated above;
   - neither `task_gid` nor `task_url` supplied → rejected as a missing required argument, same as
     today's bare positional `task_gid` requirement.

   `start` is the only one of these two commands that journals a durable `service_requests` row.
   `service_requests` stores only the request hash, not the raw arguments themselves, so precisely:
   the original `task_url` as supplied, not a resolved `task_gid`, is what's bound into that hash;
   this is deliberate, not a gap — replay identity is scoped to one `request_id` being resubmitted
   with identical arguments, never to reconciling two different calls (one by URL, one by GID) that
   happen to target the same task, so there's no requirement for those two calls to hash the same.

   Resolution must still happen early relative to everything *after* journaling, because several
   pre-dispatch steps in `execute_agent` read `task_gid` directly off the arguments before the
   command handler ever runs: pending-`start` reconciliation (`_reconcile_pending_start`) and
   Verification's open-operation lookup (`_operation_for_request`) both key off `task_gid`, and
   Verification lease acquisition depends on that lookup succeeding. Resolving `task_url` only
   inside the command handler would make those steps run against a missing `task_gid`, breaking a
   fresh Verification `start` by URL and pending-start reconciliation by URL alike. The sequence is:

   ```text
   begin_request using the original (possibly task_url) arguments
   → return any stored completed/uncertain result
   → resolve task_url to an in-memory canonical task_gid; malformed → durably complete the request
     against the original arguments via execute_agent's existing DishRuleError handling, same
     mechanism reject's reason validation already uses
   → use the canonical task_gid (not task_url) for pending-start reconciliation, operation lookup,
     and lease acquisition
   → dispatch to the command handler with the canonical task_gid
   ```

   `read` never journals at all (no `client.request_id`, no replay record) and has no equivalent
   pre-dispatch lookup, so none of this ordering applies to it — resolution can happen anywhere
   before the handler runs.

   Query strings and fragments are never scanned during resolution (see above), but the *entire*
   original URL, including any query string or fragment, is what gets bound into the request hash —
   so reject any `task_url` with a nonempty query string or fragment outright, rather than silently
   letting characters `dish` never looks at affect replay identity. This is consistent with the
   grammar's existing posture (reject unrecognized trailing segments, ports, percent-encoding)
   rather than a new exception to it. Note this means a rejected query string or fragment is
   represented only in the hash, not as readable URL text anywhere in `service_requests` — nothing
   to add here, just a reminder that the hash is the only durable record of the exact rejected
   input.

   This needs a small `command_spec`/OpenAPI update (a real input-shape addition, unlike v2's
   `create` change which only grew output), CLI argument-parser wiring for the same two commands,
   one shared parsing/resolution function used by both surfaces, and unit tests covering the URL
   forms above, the rejection cases, and both surfaces. The GPT-side instruction shrinks
   correspondingly: when Marco gives it a task URL, pass it as `task_url`; don't parse it itself. If
   Marco's message contains more than one distinct task URL, the agent asks which one rather than
   picking.

1. **The create-versus-existing decision tree — the actual v1 anti-duplicate rule.** Everything
   above resolves an *existing* task's identity; it never states when `create` is permitted at all,
   which is central, not incidental, since asking for a URL when Marco genuinely wants a new dish
   makes no sense, while treating loose wording like "create the mapo tofu dish" as sufficient
   revives the exact duplicate problem this whole design responds to. The instructions must encode:

   ```text
   recognized task_url or supplied task_gid
   → treat as an existing task; call read; never call create.

   Marco explicitly confirms no task exists yet and asks for a new one
   → call create.

   dish name only, or wording ambiguous about whether a task already exists
   → ask directly whether this is new or existing.
     existing → get its URL/task_gid, then read.
     new → get explicit confirmation, then create.
   ```

   A failed, empty, or otherwise unhelpful `read` must never be treated as evidence that no task
   exists — the agent stops and reports the `read` result to Marco rather than falling back to
   `create` on that basis.

**v1 known limitations, accepted deliberately:** natural-language name-only lookup (no URL, no known
`task_gid`) is not solved. If Marco names a dish without a URL, the agent asks him for one or for
the `task_gid` rather than guessing. Separately, because the action authority itself isn't
generalized, a resting task outside the conditions `read`/`create` can currently classify may not
give the agent a clear next-action answer; in that case the agent says so and asks, rather than
guaranteeing full autonomy in every state. `create` also has no collision protection yet — that's
v2, deliberately not rollout-blocking. All three are honest, accepted gaps, not silent ones.

**v1 landing scope (once picked up — currently soon-after-rollout, not scheduled):**

- the Custom GPT instructions rewrite (`~/honest-pantry-dish-rollout`, separate repo/rules);
- a documentation-only `deploy/gpt-action.md` update covering the existing-task `read` rule, the
  successful-`create` exception, and the complete create-versus-existing decision tree above.
  Documentation only — no schema or Action change.

`task_url` resolution (the shared parsing/resolution function, CLI/Action wiring,
`runtime-contract.md` updates, and the URL grammar/conflict documentation) is **not part of this
landing scope** — it's deferred per the status note above and only re-enters scope if real
URL-misparsing shows up.

## v2 — not blocking, no near-term timeframe

Independent of whether duplicate-creation has actually happened yet — this is a nice-to-have, not a
response to observed evidence, and not something to expect soon after rollout (see status above).

**Enumeration contract.** The collision check needs its own minimum enumeration contract — simpler
than v3's `dish_find`, since v2 only needs to answer "does a task with this exact title exist," not
present results to a human, so it needs no placement/section resolution at all:

- enumerate the entire Cooking project with full pagination;
- include completed and incomplete tasks — no `completed_since`/incomplete-only filter; a completed
  task's title still collides;
- request at minimum `gid` and `name` per task (no section/placement fields needed here);
- validate every page and pagination offset; fail closed on malformed data, a repeated offset, or a
  partial pagination failure — never compare against a partial title list;
- compare every returned title, normalized, before the creation call proceeds;
- treat any normalized match as a collision regardless of completion state.

This should be one shared internal enumeration method that v3's `dish_find` can later extend or
reuse, not a second implementation.

**Concurrency: a shared in-process lock, owned and ordered explicitly.** All governed mutations
already converge on the single `dish-service` process (`architecture.md`: "the only supported
multi-agent authority"). A lock instantiated inside a per-request command handler, application
object, or workflow service would not serialize anything, since each service request constructs its
own such objects — the lock must be owned by the long-lived `DishService` instance shared by both
listeners, not by anything created fresh per request.

Ordering matters too, not just ownership. Every replay-bound command already journals its
`service_requests` row before constructing `backend`/`DishApplication` and doing any real work — a
deliberate, universal invariant, not something specific to `create`: `dish_service/application.py`'s
`execute_agent` runs `begin_request` first for all six replay-bound commands (`create`, `start`,
`prepare`, `approve`, `reject`, `submit`), only constructing `backend`/`DishApplication` afterward,
with an explicit code comment explaining why — an invalid call must replay exactly before backend
creation, lease changes, workflow execution claims, or task/evidence mutation happen at all. The
create-serialization lock must fit inside that same invariant, not require a `create`-only exception
to it. The chosen contract is:

```text
begin the service_requests journal row
→ acquire the shared create lock (at backend/app construction, immediately before enumeration)
→ enumerate exact collisions
→ create and confirm the task
→ release lock (always, via finally — collision, enumeration failure, confirmed creation, or
  uncertain creation all release it)
```

Acquiring the lock *after* journaling relies on the same "journaled but not yet effected" recovery
machinery every other replay-bound command already needs, since real work always happens between
journaling and command-specific execution — no separate pre-journal-lock machinery, and no
`create`-only exception to the universal ordering, is required. Concretely: a process crash while a
second request is journaled but still waiting for the lock consumes that `request_id` — exact replay
fails closed with `BACKEND_UNCERTAIN`/`service_request_pending` (non-retryable,
`required_admin_action: inspect`), the same outcome any other journaled-but-not-yet-effected
replay-bound command produces. That request_id stays consumed; after inspection confirms no task was
created, a fresh attempt must use a new `request_id`, not a replay of the stuck one. The lock is one
global create mutex, not a per-title keyed lock — simpler, and proportionate at Marco's actual usage
volume; a keyed lock's added bookkeeping (cleanup of per-title lock objects) isn't justified here.

A second concurrent request simply waits, then sees the first task during its own collision check
and fails normally once it proceeds — no new dependency, no durable reservation, no
replay/conflict/restart lifecycle. This closes the same-process concurrent-agent-session race that
actually motivated this design. It does **not** close everything: a manual Asana creation or rename
between this process's enumeration and its confirmed creation can still produce two genuinely
**confirmed** duplicate tasks — not a `BACKEND_UNCERTAIN` outcome, since Dish's own creation
succeeded normally; the race is with an external actor Dish's check never saw. Similarly, if the
first request's own creation ends *uncertain* while a second request waits, the second request's
enumeration will normally find the first task, but visibility lag or an unresolved first creation
could still allow a duplicate. Neither residual case routes through the uncertain-creation/reread
recovery contract — they're their own accepted risk, owned directly by the `known-issues.md` entry
below, not attributed to a different mechanism.

Outcomes:

- one collision (any completion state) → fail, report the existing `task_gid`. Do **not** attach
  `reopen-planning` or any other continuation guidance — that can't be safely inferred from
  `completed` alone. The caller must `read` the reported `task_gid` to learn the actual required
  continuation;
- multiple collisions → fail, report all colliding `task_gid`s, same rule: no continuation guidance
  attached, agent presents the candidates and Marco selects one before any `read` happens;
- an enumeration failure during the check → fail closed, do not create.

**Title handling.** Collision normalization (Unicode NFC, trim, internal-whitespace collapse,
Unicode case-folding) is comparison-only. It never rewrites the already-validated title `create`
persists — the existing title validator already NFC-normalizes and trims input before persistence;
the collision key is a separate, stricter-for-matching-purposes derivation of that same persisted
title, not a second source of truth for what gets stored.

**Response contract.** `create`'s client-visible behavior changes, so this needs a defined, stable
collision envelope. Two refinements beyond a bare `data.colliding_task_gids` list:

- `read` is a diagnostic follow-up, not a `workflow_policy.legal_actions` transition — putting
  `"read"` into `allowed_actions` would quietly broaden that field's meaning and reintroduce a
  hardcoded authority exception. `allowed_actions` stays `[]` in both outcomes below.
- Return collision candidates as objects, not bare GIDs:
  `data.collisions: [{task_gid, title}, ...]`. The single- and multiple-collision outcomes carry
  different next-step signals, not the same `required_followup` shape stretched to cover both:
  - exactly one collision → a top-level `task_gid` identifies it directly, plus
    `data.required_followup: {action: "read", task_gid: <that gid>}` — the agent calls `read`
    directly, no selection step;
  - more than one collision → the top-level `task_gid` stays null; `data.required_followup` is
    omitted; instead `data.required_user_action: {action: "select_task"}` signals that Marco must
    pick one of `data.collisions` before any `read` happens. The GPT instructions must say
    explicitly that both signals mean a read-only resolution step is expected, even though no
    workflow mutation ever appears in `allowed_actions`.

A sensible code/rule pairing is a non-retryable `CONFLICT / create_title_collision` — the exact code
matters less than picking and documenting one stable shape. Replay semantics, stated precisely:

- response lost after a collision was stored → replay the exact same `client.request_id`, which
  returns the stored collision;
- a stored *enumeration failure* (not a collision) → correct the backend condition, then retry with
  a **fresh** request ID, not a replay of the failed one. This needs its own stable error contract
  rather than an unhandled backend exception eventually surfacing as
  `INTERNAL_ERROR / unexpected_internal_failure` — e.g.
  `BACKEND_REJECTED / create_collision_enumeration_failed`, `retryable: true`,
  `data.request_id_consumed: true`,
  `data.retry_condition: "after_backend_recovery_with_fresh_request_id"`. The exact code/rule names
  can differ at implementation time, but the response must say both that creation didn't proceed
  because enumeration was incomplete, and that this request ID now has a stored result so a retry
  needs a new one;
- the colliding task is later renamed or deleted → a genuinely new attempt uses a fresh request ID,
  never a replay of the old one.

No Action OpenAPI or command-argument schema change is needed for this response shape — `create`'s
input is unchanged, only its response `data` (already an open field) gains content.

**v2 test scope:**

- collision on the second or later enumeration page;
- a completed-task collision;
- pagination failure after an earlier page already succeeded;
- malformed pagination offsets or malformed task entries;
- no creation call ever fires after any incomplete enumeration;
- both private-CLI and Action-surface `create` requests contend on the same shared lock;
- a second request waits while the first is creating, then correctly sees the first task once it
  proceeds;
- a shutdown or injected failure while a second request is waiting on the lock;
- the lock releases after every outcome — collision, enumeration failure, confirmed creation, and
  uncertain creation alike;
- exact replay behavior after a request that had already journaled and was queued (waiting on the
  lock) gets interrupted — assert the precise outcome: `BACKEND_UNCERTAIN` /
  `service_request_pending`, `retryable: false`, `required_admin_action: inspect`, and that the
  consumed `request_id` cannot be reused for a fresh attempt;
- one collision, multiple collisions, and a clean no-collision create.

**v2 landing scope:**

- `create`'s collision-check implementation, its shared enumeration method, the shared
  `DishService`-owned lock (acquired after journaling, at backend/app construction, released in
  `finally`), and the normalization function;
- `architecture.md`, since a process-shared create mutex and its relationship to request-journal
  ordering is real concurrency architecture, even without a new Action or durable authority;
- `runtime-contract.md`, documenting the collision envelope, the `required_followup`/
  `required_user_action` split between the single- and multiple-collision outcomes, and the precise
  three-way replay semantics above;
- `deploy/gpt-action.md`, documenting both the `required_followup`-triggered direct `read` and the
  `required_user_action`-triggered selection-before-`read` step as expected read-only resolution
  steps even when `allowed_actions` is empty;
- a `known-issues.md` entry owning the two residual risks directly: a manual Asana edit/rename or an
  uncertain first-creation racing a waiting second request can still produce a confirmed duplicate;
  the duplicate, if it ever occurs, is visible and manually removable; reconsider only on an actual
  recurrence; durable cross-process uniqueness belongs naturally with the database-backed task store
  instead of bespoke reservation machinery now;
- the test scope above.

## Deferred (v3)

Everything below is **draft design only** — not scoped for implementation now. It exists so that if
v1/v2's known limitations (name-only lookup, incomplete action-authority coverage) turn out to cause
real recurring friction, the design work is already done rather than starting cold. Track this as a
`future.md` near-term candidate that points back here; do not implement without revisiting that
evidence bar first.

### Decision summary (v3)

Two further additive changes, on top of v1 and v2:

1. Generalize the shared action authority, not `read` itself, to cover every task condition, not
   only "has an active operation" and "just created." See "Task-level action authority" below —
   including making `create` project this same authority after confirmed creation, rather than
   hardcoding `allowed_actions`/`required_start_kind` as it does today. `read` and `start` both
   consume the same authority result; neither independently reconstructs legality.
1. Add one new bounded, read-only Action — provisionally `dish_find` — that resolves identity only:
   a free-text dish-name query to candidate `task_gid`s, using deterministic title matching.
   `dish_find` never determines workflow state or legality; every selected candidate still goes
   through `read`, unconditionally, including when it's completed. Would be available to Claude and
   Codex as well as GPT — the same lookup gap applies whenever any of them starts a fresh session
   without an already-known `task_gid`.

### Goals (v3)

- Let Marco open a fresh agent session and refer to a dish by name, not by `task_gid`, without
  needing a URL.
- Never resolve ambiguity silently: auto-resolve only on an unambiguous single exact-title match;
  whenever more than one plausible candidate remains after the agent's narrowing pass, surface that
  narrowed set for Marco to pick from explicitly.
- Preserve every existing invariant in `architecture.md`: one action authority
  (`workflow_policy.legal_actions`-equivalent stays the only source of legal transitions), the
  bounded Action surface, no semantic judgment inside the deterministic tool, and Cooking placement
  selected only by Cooking project GID, never by first membership.
- Give the agent one reliable rule for "what's next" — `read` — that holds for every task state, by
  making the underlying authority actually cover every state rather than special-casing `read`'s
  output.

### Non-goals (v3)

- Semantic or meaning-based matching *inside `dish_find` itself* (e.g. the tool inferring "that
  spicy tofu thing" means Mapo Tofu by understanding the dish, rather than by title-text
  similarity). Excluded for predictability and boundedness, not because it falls under
  architecture.md's "automatic semantic recipe judge" prohibition — that line is about judging
  recipe content/correctness, a different concern. The GPT's own downstream narrowing over
  `dish_find`'s returned candidates is explicitly in scope — see "Two-layer filtering" below.
- A general query/search API over arbitrary Asana fields, comments, or notes.
- Any change to the workflow *lifecycle* — the stage sequence and its transition rules are
  unchanged, and `dish_find` remains read-only, same category as `dish_read`/`dish_sections`. What
  **is** in scope is *how many task conditions the shared authority can classify* — today it only
  classifies two; this design requires it to classify all of them. That is a real expansion of the
  authority's coverage, not merely `read` plumbing.
- Removing `task_gid` as the addressing identifier. This only adds a lookup path to it.
- A durable normalized-title reservation/serialization mechanism to make duplicate prevention an
  actual cross-process guarantee. Out of scope even in v3 — v2's in-process lock already closes the
  ordinary concurrent-agent-session race; only a manual Asana edit/rename or a crash mid-check
  remains accepted risk, and that residual case doesn't justify durable reservation machinery.

### Task-level action authority

The existing authority (`workflow_policy.legal_actions` and its snapshot) is operation-shaped: it
returns legal actions only when a task has an active operation. `create` today does not even go
through it — it hardcodes `allowed_actions=["start"]` and `required_start_kind="planning"` directly
in the command handler. This design needs a full task-level snapshot — call it
`TaskWorkflowSnapshot` — that `read`, `start`, *and* `create` (after confirmed creation) all
consume, so no caller ever hardcodes or special-cases a condition the authority doesn't already
classify.

**Precedence when conditions overlap** (evaluated top to bottom; the first matching rule wins):

1. Live content or placement conflicting with durable task state → fail-closed drift/reconciliation
   result, regardless of any other condition.
1. A terminal operation with unresolved external-effect evidence, or stale cleanup-lease evidence
   that hasn't been proven safe → recovery guidance; no ordinary action exposed.
1. Malformed or ambiguous Cooking-project placement → no governed action; a structural placement
   diagnostic, not a workflow classification (not the same condition as an intentionally excluded
   section).
1. An old task-schema version → migration guidance, not ordinary `start`.
1. A completed task with an interrupted Planning reopen → the reopen's own recovery guidance, not
   ordinary `reopen-planning` guidance.
1. Task in an excluded/unmanaged section → no governed action.
1. Task has an active operation → unchanged, existing operation-scoped `legal_actions` behavior.
   Verification's `start` requires an existing open operation and fails `open_operation_missing`
   without one, so a task with no open operation is never legitimately routed toward
   `kind: verification` — a terminal Planning-to-Research handoff with no open operation is instead
   the valid-Planning-brief base row below, not this rule.
1. None of the above → classify by the base table below.

**Base classification table** (reached only once rules 1–7 above don't apply):

| Task condition                                                                                                | `allowed_actions` | `required_start_kind` | `required_admin_action` | Notes                                                                                                                                                                                                                                                      |
| ------------------------------------------------------------------------------------------------------------- | ----------------- | --------------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| incomplete bare task, never Planning-started                                                                  | `start`           | `planning`            | —                       | —                                                                                                                                                                                                                                                          |
| completed bare task                                                                                           | —                 | —                     | `reopen-planning`       | —                                                                                                                                                                                                                                                          |
| valid Planning brief, handoff not yet started                                                                 | `start`           | `initial`             | —                       | matches existing documented cross-stage-handoff behavior                                                                                                                                                                                                   |
| malformed or partial Planning brief                                                                           | —                 | —                     | —                       | exact validation findings returned instead                                                                                                                                                                                                                 |
| current-schema canonical task, no open operation, durably approved/submitted baseline proven by Dish evidence | `start`           | `change`              | —                       | resolved via the existing `kind: change` start-variant ("start a post-signoff change operation"). "Proven by Dish evidence" means durable signoff/submission records, not merely that the live document text says "ready" or sits in a destination section |
| current-schema canonical task, no open operation, no exact durable approved/submitted baseline                | —                 | —                     | —                       | no ordinary action; reconciliation guidance instead — the live document must never substitute for durable signoff/submission evidence                                                                                                                      |

Both `read` and `start` must consume one shared `TaskWorkflowSnapshot` result — never derive or
check any row of this table independently — and `create` must project the same snapshot for the task
it just made, rather than hardcoding its response.

### `dish_find`: proposed shape

Read-only, same category as `dish_read`/`dish_sections` (no `client.request_id`, no replay record).
`dish_find` resolves identity only — it never computes or reports workflow state, legality, or
`required_admin_action`. Every selected candidate, regardless of completion state, is followed by an
unconditional `read` call, which is the only place workflow continuation is determined.

`dish_find` has two independent scope axes — do not conflate them:

- **Section scope** (which sections are searched): whole Cooking project, no section exclusions, for
  both the exact and fuzzy tiers.

- **Completion scope** (which completion states are searched): the *fuzzy* tier is incomplete-only
  by default, since Marco does not want completed tasks surfaced while browsing. The *exact-title*
  tier always covers all tasks, completed included, because exact-title collision detection must not
  be scopable away.

- **Input:** `agent`, a free-text `query` string, and an optional `fuzzy_section_gid` filter — a
  section GID, not a name or arbitrary location text, since Cooking placement authority is
  GID-based. The field name makes its limited scope (fuzzy tier only) unmistakable; the exact-title
  tier's section and completion scope are both fixed and ignore this filter entirely.

- **Placement extraction and malformed placement — one contract, not two alternatives:**
  task-to-section mapping uses the same Cooking-project-GID-filtered placement rule as the existing
  exact-task path — filter memberships by the Cooking project GID, never use first membership, reuse
  the existing placement parser rather than writing a second implementation. Complete project
  enumeration determines matches in both tiers; **no matching task is ever omitted from either tier
  because its placement is ambiguous** — title collision and placement validity are separate
  questions, and dropping a task from match detection could hide a real collision from both
  `dish_find`'s caller and `create`'s collision guard. A task whose placement can't be resolved is
  still returned, with `section_gid: null`, `section_name: null`, and a structured `placement_error`
  field populated — in either tier, using the same fields, so no separate omission-tracking schema
  is needed. Only an **incomplete** project enumeration (e.g. a pagination failure) fails the whole
  command; a resolvable-but-ambiguous individual task's placement never does. This is also its own
  precedence row in the task-level authority table above.

- **Enumeration mechanism:** `runtime-contract.md`'s documented "Asana section enumeration follows
  all pages" only covers listing *sections*, not the tasks inside them — that capability does not
  exist yet. `dish_find` needs a new, fully paginated project-task enumeration path (task title +
  Cooking-GID-filtered section + completion flag per task). If pagination fails partway through,
  return an error — never a partial result presented as if it were authoritative. No caching is
  needed at Marco's task volumes; each call re-enumerates fresh.

- **Bounding, since this is a public Funnel-exposed Action surface, not an internal tool:**

  - reject blank queries and queries below a minimum length (e.g. 2 characters);
  - enforce a maximum query length;
  - enforce a hard maximum candidate count on the *fuzzy* tier only, with an explicit
    `fuzzy_truncated: bool` flag when the cap is hit (exact matches are never truncated — see
    "Response schema" below);
  - deterministic ordering and tie-breaking (e.g. `match_score` descending, then `task_gid`
    ascending);
  - validate `fuzzy_section_gid` against the real section registry rather than accepting arbitrary
    text;
  - **on repeated-query enumeration risk:** a candidate cap and query-length limit bound one
    response; they cannot prove that many repeated crafted queries can never reconstruct the
    project's title list. No rate limiting or query-budget mechanism is proposed. Given the private
    single-user Action token and personal-scale deployment, corpus enumeration via patient repeated
    querying is an accepted risk for now, consistent with comparable risks this tool already accepts
    (per `known-issues.md`) — not something this design claims to prevent. Revisit only on real
    evidence of abuse.

- **Exact-match normalization** is the same function defined in v2's `create` collision check above
  — never a second implementation of it.

- **Two-layer filtering.** `dish_find` itself stays dumb, deterministic, and deliberately permissive
  on the fuzzy tier — tuned toward high recall over high precision, so it under-filters rather than
  risks silently dropping the intended task. The GPT applies a second narrowing pass over that
  candidate set using its own judgment before anything reaches Marco, reducing a noisy tool-returned
  list to the plausible subset. This does not reintroduce semantic matching inside the deterministic
  tool: the tool's output stays string-similarity-based and inspectable; only the agent's own
  downstream reasoning over that output is semantic. Scorer is decided as `rapidfuzz`'s
  `token_set_ratio` specifically (not `WRatio` — picking one, since they're materially different
  scorers); preprocessing is the same case-folding and whitespace-collapse used for exact
  normalization, applied before scoring. The permissive-threshold value is still open — see Open
  questions.

- **Response schema**, separating tiers explicitly rather than one ambiguous candidate list:

  ```text
  data.query
  data.normalized_query
  data.exact_matches[]
  data.fuzzy_candidates[]
  data.fuzzy_truncated
  ```

  Each entry in both arrays carries at least:

  ```text
  task_gid
  title
  section_gid          nullable — see placement note above
  section_name         nullable
  placement_error      nullable
  due_on
  completed
  match_type           exact | fuzzy
  match_score          fuzzy entries only; omitted/null for exact entries
  ```

  `completed` is a formal, always-present field — the flow depends on it, so it belongs in the
  schema, not just prose. `exact_matches` has no truncation flag because it must be complete or the
  command fails closed — a truncated exact tier could hide the very collision this design exists to
  catch. If the exact tier itself would exceed a safety bound, return an explicit error rather than
  a partial exact list. Only `fuzzy_candidates` is capped and flagged via `fuzzy_truncated`.
  `fuzzy_section_gid` applies only to `fuzzy_candidates`; `exact_matches` is always computed
  project-wide regardless.

- **Exact/fuzzy precedence:** exact matches always take priority over fuzzy ones.

  - Exactly one normalized exact-title match, and no other exact matches → auto-resolves, no
    confirmation needed — but still always proceeds to `read`, never skips it.
  - More than one exact-title match (duplicate titles exist) → present the exact matches as a
    numbered list and require confirmation; never auto-pick among them.
  - Zero exact matches → fall through to the agent's narrowed fuzzy candidate set: a single narrowed
    match asks Marco to confirm it; more than one is presented as a numbered list (`1.`, `2.`, `3.`,
    ...), including the option that none of them is it.
  - `dish_find` never silently ranks-and-picks a winner across multiple candidates at any tier, and
    never uses completion state to infer workflow legality, continuation, or which returned
    candidate to select — completion only ever scopes the default fuzzy tier and appears as a
    presentation field; `read` is the only place any of those decisions is made.

### One live snapshot per invocation

Within one `read` or `start` invocation, the displayed task facts (title, completion, placement),
schema/content validation, durable-state comparison, and `TaskWorkflowSnapshot` legal-action
classification must all derive from one single complete live-task reread — not from separate reads
stitched together, which could otherwise let displayed placement come from one Asana read,
validation from another, and legal actions from a third. The same rule applies to `create`: after
confirmed creation, it derives its task facts and projected `TaskWorkflowSnapshot` from the same
confirmed live task returned by the creation sequence itself (creation is a multi-call external
effect, not a single transaction), not from a separately assembled task state. A later, separate
mutation invocation rereads afresh and reasserts legality independently; this rule governs internal
consistency *within* one invocation, not across invocations.

### Duplicate prevention at `create` (v3 note)

`create`'s collision check stays exactly as defined in v2 — best-effort, not a guarantee, for the
same reasons. `dish_find` returning "no match" in v3 is likewise advisory only, for the same race
reasons already accepted in v2: it does not add a reservation, conflict, or serialization mechanism,
so no "title reserved by another in-flight request" outcome exists in this design — that outcome
would require the reservation mechanism this design deliberately does not build, and belongs only in
a hypothetical future serialized redesign, not in this outcome list.

### Resolution sequence (v3)

The full flow a GPT session follows for any dish reference, once `dish_find` exists:

1. `task_gid` already known (from earlier in the conversation, a URL per v1, or supplied by Marco) →
   call `read` directly.
1. Only a dish name is known → call `dish_find` (which always checks the exact-title tier across all
   tasks and completion states, alongside the fuzzy tier's incomplete-only-by-default, whole-project
   scope).
   - one exact-title match, no other exact matches → call `read` on that `task_gid` (regardless of
     its `completed` flag — `read` decides what happens next, `dish_find` does not);
   - more than one exact-title match (any completion state) → present the numbered list, get
     confirmation, then `read` on the confirmed `task_gid`;
   - no exact match, one narrowed fuzzy match → ask Marco to confirm before calling `read`;
   - no exact match, multiple narrowed fuzzy matches → present the numbered list, ask which one (or
     none), then `read` on the confirmed `task_gid`;
   - Marco rejects every candidate in a narrowed set → do not proceed toward `create`. Either show
     the full raw candidate set `dish_find` returned before the agent's narrowing pass, or re-run
     `dish_find` with a revised query — Marco's choice, not the agent's default;
   - genuinely no match at all → confirm with Marco, then call `create`. `create`'s own best-effort
     collision check runs at that point — it is not a guarantee, and this design accepts that
     explicitly.

### Open questions (v3)

- The permissive-threshold value for `rapidfuzz`'s `token_set_ratio` — needs tuning against real
  Cooking-project titles once implemented, not a value to guess now.
- The exact bounding parameter values for `dish_find` (minimum/maximum query length, maximum fuzzy
  candidate count) — reasonable starting values, tuned later against real use, not a blocking
  decision.

### Landing scope (v3)

Because this adds a new runtime surface and generalizes an authority boundary, implementation is not
complete with just the tool code. Per `architecture.md`'s own rule ("update this document in the
same commit when a change... adds a runtime surface... or changes which component owns a durable
fact"), the same landing must also update:

- `architecture.md` and `runtime-contract.md`;
- `deploy/gpt-action.md`;
- `dish_service.command_spec`, and the generated/checked-in Action OpenAPI it drives;
- HTTP dispatch and authentication-surface tests for the new Action;
- test coverage for pagination, malformed/ambiguous Cooking-membership handling (including that it
  never silently drops exact- or fuzzy-tier candidates for placement reasons), exact-match
  normalization, the fuzzy candidate cap/truncation behavior, and the task-level authority's
  precedence ordering.

## Not in scope here

The Custom GPT instructions rewrite (state-driven Action selection, part of v1) is a
`~/honest-pantry-dish-rollout` change, governed by that repo's own protocol-doc rules, not this
file.
