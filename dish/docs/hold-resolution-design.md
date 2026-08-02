# Evidence / Human Review inline resolution

Design triage, not implementation authorization — same status as other `*-design.md`
documents referenced from [`future.md`](future.md).

## 1. Problem (Marco)

Evidence and Human Review holds assume an asynchronous handoff: a blocking CLI round-trip
(`dish-admin supply-evidence` / `record-human-decision`) that requires Marco to run a
separate command after the decision has already been talked through. In practice Marco is
usually live in chat with the agent when a hold is raised. Requiring a hold at all in that
case — write the hold, then run a second command to release it — is unnecessary ceremony:
when Marco is present and the same run can finish the job, the agent should just ask, get
an answer, and continue, not force a round-trip through durable hold state.

The hold mechanism (`held_evidence` / `held_human`) still earns its keep for the genuinely
asynchronous case: the run ends, the agent needs to check something, or Marco says "later."

## 2. Requirements (Marco, as agreed in conversation)

| # | Requirement | Problem it fixes |
|---|---|---|
| 1 | Talk first, record second | The agent must have a real conversation and only draft a decision after Marco has actually decided, ideally reading it back first |
| 2 | Distinct approve vs. reject outcomes | Approval and rejection currently share one free-text field; need a structured outcome the next verifier can't misread |
| 3 | Durable hold = cross-run only | When Marco is live in the same run, the agent should just ask and continue — the formal hold/resume round-trip is only for when the fact must outlive this run (handoff, lease expiry, later continuation) |
| 4 | Human Review defaults to synchronous, same as #3 | The normal path is talk it through live, then one sign-off command — async (Marco wants to think it over) is the supported exception, not the default assumption |

## 3. Proposed design (mine, revised after external review)

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

  **Needs critiquing — Marco questions this conclusion (2026-08-02 discussion).** The
  claim above is an AI-drawn conclusion, not something Marco independently verified or
  agreed is actually correct; it should be re-examined rather than taken as settled before
  this design proceeds further.

`held_evidence`/`held_human` remain in the system under this design, reserved for the
genuinely async case — run ends, needs research, Marco says "later" — which is also the
only thing `dish-admin holds` needs to list.

## 4. Sequencing relative to the database-backend migration

A large share of why resolution feels heavy today is the Asana round-trip: every mutation
fetches live Asana content, hashes title+notes into a content identity, and confirms it
still matches what was inspected before touching anything — the drift-detection and
placement-matching machinery exists because canonical content lives in an external,
independently-editable system. Once storage is fully internal
(see [`database-backend.md`](database-backend.md) and its companion documents), an inline
resolve-and-create-new-cycle operation becomes one local transaction — update a row, insert
a new cycle row — instead of read-Asana, hash, compare, write-Asana, re-verify. This design
should be sequenced after that migration rather than before it if there's a real choice in
ordering; see the flag in [`future.md`](future.md).

## 5. Scope note

Beyond the new command itself, this touches: `reject`/`approve` Action arguments and the
generated OpenAPI where they interact with the new continuation, `admin_cli.py` and admin
dispatch, command templates returned by `read`/`inspect`, `architecture.md` and
`runtime-contract.md`, and both the checked-in GPT template and the separate live
custom-GPT instructions (per this repo's `CLAUDE.md` sync requirement). "Talk first" (#1)
and "synchronous is the default" (#3, #4) also need the corresponding wording in
`dish-verification-protocol.md`/`-compact.md` and the live GPT instructions, since the tool
cannot verify a conversation happened — that part is enforced by protocol text, not code.
