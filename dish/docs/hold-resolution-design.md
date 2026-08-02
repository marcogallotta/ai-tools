# Evidence / Human Review hold resolution redesign

Design triage, not implementation authorization — same status as other `*-design.md`
documents referenced from [`future.md`](future.md). Written after a live incident where a
Human Review report gave no quantified blocker and no clear path for Marco to negotiate
instead of just approve/reject.

## 1. Problem (Marco)

The current Evidence and Human Review mechanism was designed as if Marco is not present —
a single blocking CLI round-trip (`dish-admin supply-evidence` / `record-human-decision`)
that assumes an asynchronous handoff. In practice Marco is usually live in chat with the
agent. The system currently:

- Gives no quantified magnitude for a blocked limit ("cannot plausibly remain at or below
  40g fat" — no indication of how far over).
- Treats the resolving command as the interaction itself, rather than the last step of a
  conversation.
- Has no structural distinction between "approved," "rejected — do X instead," and
  "rejected, no fix yet" — all three collapse into one free-text `--detail` field appended
  to the task's permanent `Decisions` list. The next verifier has to infer which one
  happened purely from reading prose, with no backstop if it misreads.
- Has no way to list what's currently outstanding across tasks — held tasks stay in their
  existing Asana section with no visible marker beyond text inside the task body, so Marco
  loses track of what's waiting on him.
- Has no protection against a command being run against the wrong task when multiple
  tasks/agents are open at once.

## 2. Requirements (Marco, as agreed in conversation)

| # | Requirement | Problem it fixes |
|---|---|---|
| 1 | Quantify the blocker | Reports must state the actual computed value and the delta over/under the limit, not just a verdict |
| 2 | Talk first, record second | The agent must have a real conversation and only draft `--detail` after Marco has actually decided, ideally reading it back first |
| 3 | Distinct approve vs. reject outcomes | Approval and rejection currently share one free-text field; need a structured outcome the next verifier can't misread |
| 4 | Evidence hold = cross-run only | When Marco is live in the same run, the agent should just ask and continue — the formal hold/resume round-trip is only for when the fact must outlive this run (handoff, lease expiry, later continuation) |
| 5 | List outstanding holds (Evidence *and* Human Review), with an Asana link per entry | No command currently lists what's waiting on Marco; needs task title, gid, an Asana link, hold type, and the actual question text |
| 6 | Guard against wrong-task pastes | Nothing currently ties a resolution command to what's on screen; a mismatch should error instead of silently applying |
| 7 | Human Review defaults to synchronous, same as #4 | The normal path is talk it through live, then one sign-off command — async (Marco wants to think it over) is the supported exception, not the default assumption |

## 3. Proposed solution (mine, revised after external review)

An earlier draft of this document proposed three parallel directions (A/B/C, patch vs.
split vs. full fold-in). External review (ChatGPT, reviewing the repo's actual hold
implementation, admin transport, and persistence model) found that draft understated in
several ways how much the resolution mechanism needed to change, and caught concrete
implementation problems worth recording rather than re-discovering later. The plan below is
the corrected version, split into two phases instead of three alternatives.

### Phase 1 — observability and safer durable holds (near-term, no schema migration)

Ships without touching the Verification state machine or requiring a SQLite migration:

- **`dish-admin holds`**: a read-only list of every open hold, correctly classified rather
  than treated as one uniform type. The implementation has at least three distinct cases —
  pre-construction Research holds, ordinary Verification Evidence/Human Review holds, and
  the automatic two-pass Verification hold (which already requires `dish-admin reopen`, not
  `record-human-decision` — see `workflow_policy.py:80-87`). Each listed entry must return
  its actual `required_admin_action`, not assume one from `held_evidence` vs. `held_human`
  alone, plus task title, task_gid, an Asana link, and the question text. Pre-construction
  holds don't write a marker into the task body at all — their question comes from the
  `research_preconstruction_hold` operation step, not `Status detail`.
- **Quantified blocker fields, attached at hold creation, not resolution.** The missing
  number is a property of the `reject` call that raises the blocker (`step8.py:811-865`),
  not the later admin command that resolves it — putting it on the resolution command
  records the number after the under-specified report has already reached Marco. Structured
  fields (metric, actual, limit, delta, unit, basis), required only when the blocker is an
  actual quantified limit — a missing source citation has no meaningful magnitude and
  shouldn't be forced into this shape. This does not need a new column: `operation_steps`
  and the audit log already store arbitrary JSON (`declare_operation_step`,
  `record_audit`), so these are new keys in payloads that already exist, validated in
  Python before the call is accepted.
- **A real wrong-paste guard.** A task-title match is not a safety invariant — a pasted
  command from the wrong chat carries a self-consistent title+id pair, so title-matching
  wouldn't catch the actual failure mode. `verification_cycles` already carries
  `hold_content_version_id` / `hold_identity` / `hold_section_gid` from the existing hold
  binding trigger; the resolver should display and cross-check those stable identifiers
  (task_gid, operation_id, cycle_id/hold fingerprint) rather than inventing new storage, and
  reject a stale or mismatched hold outright.

Explicitly **not** in Phase 1: a four-value outcome enum (approved/rejected-fix/
rejected-open/deferred). Those conflate three different things — disposition, whether the
hold stays open, and remediation — and the transition semantics (an "open" or "deferred"
update conflicts with `hold_resolution_decision` being a one-shot, immutable operation
step) need to be worked out before this is safe to add. Requirement #3 is served in Phase 1
only by the magnitude/classification work above, not by a new enum.

"Talk first" (#2) and "synchronous is the default" (#4, #7) are protocol-doc requirements
on agent behavior in Phase 1 — the tool cannot verify a conversation happened, so this is
enforced by updating `dish-verification-protocol.md`/`-compact.md` and the live GPT
instructions, not by new code.

### Phase 2 — Marco-authenticated inline resolution (the real synchronous fix)

A new Marco/admin-only command, callable directly from the live state — `prepare_required`
for pre-construction Research, or `await_verification` for a currently inspected
Verification cycle — **without ever entering `held_evidence`/`held_human`** in the first
place. It records the decision, appends the Decision to the task, and establishes the next
Research or Verification continuation in one call, eliminating the round-trip for the
common case where Marco is right there.

This must be a distinct Marco/admin-authenticated command, not folded into the agent's own
`reject`/`approve` Action call. Two reasons, both structural:

- **Authority boundary.** The GPT Action has no admin credential and never receives one
  (`runtime-contract.md:307-321`, `deploy/gpt-action.md:1-5`). Accepting a human decision
  through an agent-authenticated `reject`/`approve` call would mean Dish trusts an
  agent-authenticated assertion that Marco decided something, which is a real authority
  change, not just a new entry path, and isn't proposed here.
- **Content identity.** Verification is bound to one exact inspected content identity.
  Appending a Marco Decision changes the canonical task identity, so the old inspection
  can no longer stand as evidence for the resulting content — the existing resolver
  correctly creates a new Verification cycle for this reason (`step8.py:1347-1412`). An
  inline decision can avoid ever entering `pending-human-review`, and can record the
  Decision and open the next cycle in one call, but it cannot turn the *current* cycle
  directly into `ready`; the new identity still needs its own inspection and approval.

`held_evidence`/`held_human` remain in the system under this design, reserved for the
genuinely async case — run ends, needs research, Marco says "later" — which is also the
only thing Phase 1's `dish-admin holds` needs to list once Phase 2 ships.

### Phase 2 is cheaper after the database-backend migration

A large share of why resolution feels heavy today is the Asana round-trip: every mutation
fetches live Asana content, hashes title+notes into a content identity, and confirms it
still matches what was inspected before touching anything — the drift-detection and
placement-matching machinery exists because canonical content lives in an external,
independently-editable system. Once storage is fully internal
(see [`database-backend.md`](database-backend.md) and its companion documents), an inline
resolve-and-create-new-cycle operation becomes one local transaction — update a row, insert
a new cycle row — instead of read-Asana, hash, compare, write-Asana, re-verify. Phase 2
should be sequenced after that migration rather than before it if there's a real choice in
ordering; see the flag in [`future.md`](future.md).

## 4. Scope note

Even Phase 1 touches more than the two admin handlers: `reject` Action arguments and the
generated OpenAPI (if blocker fields are added there), `admin_cli.py` and admin dispatch,
command templates returned by `read`/`inspect`/`reject`/blocked `start`, hold-resolution
and partial-execution reconstruction tests, `architecture.md` and `runtime-contract.md`,
and both the checked-in GPT template and the separate live custom-GPT instructions (per
this repo's `CLAUDE.md` sync requirement). None of this requires a database migration, but
none of it is a single-file change either.
