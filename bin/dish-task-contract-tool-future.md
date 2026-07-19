# Dish Task Contract Tool — Future / v2+ Ideas

**Purpose:** Holds everything about the dish-task contract tool that is NOT part of the v1 design —
the v1b enforcement flip, v2 candidate features, and ideas considered and rejected outright. Split
out of `dish-task-contract-tool.md` so that doc can stay focused on exactly what v1 needs to exist
and work.

**Status:** Not authorized for implementation. Nothing here is scheduled; items move into the v1 doc
(or a future v1.x/v2 doc) only when Marco explicitly decides to build them.

## Versioning plan

The tool is built and rolled out in stages, scoped to what the evidence in `dish-docs-design.md`,
`dish-incident-log.md`, and `dish-review-log.md` actually requires. Nothing beyond v1a/v1b is built
until real usage data justifies it.

**v1a — build and soft-launch.** The full guarded path (`prepare` / `approve` / `reject` / `submit`
/ `contract-admin recover`) is implemented, tested, and usable end-to-end against live tasks — it
performs real Asana writes through the guarded, token-protected path. What v1a does *not* do is make
this path mandatory: the existing generic Asana CLI still works for managed tasks, and its
managed-task check runs in advisory/log-only mode (see Contract-managed task registry, Logging and
observability). This proves the hardest, most novel logic — the structural validator against the
contract's manifest, the exact-content hash binding, the submission state machine, and
uncertain-outcome recovery — under real conditions, without the operational risk of a validator bug
or an over-sensitive staleness check blocking a live cook. It also produces the usage data needed to
decide v1b's timing and v2's scope.

**v1b — enforce.** Once v1a's validator has run clean against real usage and the `modified_at`
staleness behaviour has been empirically confirmed (see Content hashing), the generic CLI's
managed-task check is flipped from advisory to blocking. No new mechanism is added at this stage —
v1b is a configuration flip on v1a's own logged evidence, not new code.

**v2 — add once v1a data justifies it.**

- The two-failed-pass stop rule (`dish-task-contract.md` lines 206-209) — real contract text, but no
  incident shows it failing in practice; v1a's rejection-rate logging is exactly the evidence needed
  to decide whether to build it. Cheap to add once needed (a counter and a gate).
- The small-change (`small`/Local) carelessness speed bump — Marco's standing concern (see Out of
  scope (all versions) in `dish-task-contract-tool.md`): an honest agent carelessly mis-declaring a
  material change as `small`, not a malicious one gaming the system. Designing its trigger condition
  needs data on what real `small`-declared diffs actually touch and how large they are — but v1a
  does not collect that (see Deferred below: diff-summary fields are dropped from v1a entirely, and
  only rule-level pass/fail logging is kept). So this item isn't just waiting on v1a's existing logs
  to accumulate; it needs diff-summary computation added first, as its own v1.x step, before the
  trigger condition can be designed from real data.
- Bounded direct-dependency surfacing (see Direct dependencies) — already scoped in the change plan
  as advisory and non-blocking; natural to add once the core write path is proven.
- Token/submission replacement as a distinct action from `contract-admin recover` — only worth
  building if recovery proves insufficient in real use.
- Replacing the manual ChatGPT copy/paste relay (`dish-task-contract-tool.md`'s ChatGPT workflow
  section) with a custom GPT Action calling a live endpoint directly, cutting out the local
  human/agent relay step. Marco already has the underlying pattern proven on a laptop-hosted custom
  GPT for another purpose (Action endpoint, hosting, schema registration), so this isn't a new infra
  bet — but it's punted to v2 by Marco's explicit call, not incident evidence: it changes the trust
  model (ChatGPT calling directly into a live endpoint against real tasks, vs. today's model where
  nothing executes until a local agent runs `contract prepare`/`submit`) and what `Self-verified:`
  actually asserts (ChatGPT's own claim in its output vs. something the Action layer stamps on its
  behalf). Decide the trust/semantics question before building.

**Dropped, not deferred.** These were considered and rejected outright, not postponed:

- Verifier in-place editing and any author-reassignment bookkeeping (`last_content_author` and
  related). No incident motivates letting a verifier edit the note at all; a verifier who finds a
  problem rejects it and the editor resubmits (see Workflow). This removes an entire dimension of
  state the design previously carried.
- `--confirm-independent-review` as a separate required flag, and any dedicated "self-verification
  collision" detection alongside it. The opposite-family requirement on `approve` already makes
  `editor_agent == verifier_agent` structurally unreachable — routing rejects it before any
  collision check could run — so a comparison built to catch that case is dead code, not a
  protection. The real residual risk (one session dishonestly declaring `claude` for editing and
  `gpt` for verification) is exactly the "trusted, not authenticated" limit already stated in Scope,
  and no mechanical check catches it; claiming one does would be worse than naming the gap.
- A cached, authoritative `managed_tasks` table. Management is always resolved live (see
  Contract-managed task registry); a cache that isn't authoritative isn't worth maintaining.
- A distinct adversarial self-review mechanism. The review log is explicit that this "was an
  assistant recommendation and was not approved in the enforcement handoff" and creates no
  implementation requirement. Stays out unless Marco separately approves it.
- Cryptographic identity authentication, recursive dependency audits, automatic migration of
  existing tasks, a multi-user/remote trust service (see Out of scope) — no new reason to revisit
  these.

## Deferred: write-safety and observability machinery with no evidenced incident (v2)

Checked against `dish-incident-log.md` and `dish-review-log.md` in full: none of the following is a
response to any recorded incident. v1a ships the simpler behaviour noted under each; build the
fuller mechanism only if v1a's own logging actually shows a problem.

- **`write_count` escalation (silent write / `--final`-gated write / hard block /
  `contract-admin reset`).** No incident involves an accidental duplicate write, a repeat-write
  failure, or anything else this guards against. v1a: `submit` performs exactly one write per
  submission and logs it; no confirmation round-trip, no reset mechanism.
- **`contract-admin recover`'s full crashed/uncertain-outcome table.** No incident involves a
  crashed process or an ambiguous Asana API outcome. v1a: an uncertain `submit` outcome is logged
  and left for Marco to check directly in Asana; the deterministic recovery table is only worth
  building once a real crash/ambiguous-outcome case actually occurs.
- **Diff-summary fields (`characters_added`, `characters_removed`, `lines_changed`,
  `headings_touched`) computed at `prepare`.** No incident needed this granularity. What the
  evidence actually supports is rule-level pass/fail logging (which validation rule failed, how
  often) — that stays in v1a; the per-character diffing does not.
- **The fuller periodic-summary query list** (small-change diff characterization, `--final`/reset
  frequency). These feed decisions (the v2 small-change speed bump's trigger, the write-limit's
  level) that don't exist yet because the machinery they'd tune isn't being built in v1a either.
  v1a's logging keeps only what's evidenced: validation-failure rate by rule, and advisory-bypass
  count.

## Direct dependencies (v2)

Deferred; not built in v1a/v1b. Dependency surfacing is advisory and must not block submission
status in any version. A later scanner may surface only bounded direct candidates: exact task-GID
references; explicit Asana links; exact task-name references; clearly named planning documents. It
must not recursively audit dependencies or decide semantic impact.

## Candidate deterministic checks flagged during dish-verification.md checklist drafting (v2)

Raised while drafting `dish-verification.md`'s compact checklist in honest-pantry
(`dish-docs-design.md`, "Research/verification split and file rename"), cross-checking it against
this tool's v1a validation list. Not evidenced by a dedicated incident beyond the two named below;
recorded as candidates because both are mechanically checkable against note text without any
semantic/culinary judgment, unlike most other deferred items in this file.

- **Blocking-dependency bracket-marker presence (incident 22).** Check that a task carrying a
  practical/Human blocker also carries `[bracketed]` marker text on its title/marker line. Purely
  structural (regex/string check against the marker line), same character as the existing
  no-headings-outside-manifest check, not a judgment of whether the blocker itself is real or
  correctly described.
- **`WHAT TO BUY` / `QUANTITIES` quantity equality (incident 21).** v1a only checks that
  `WHAT TO BUY` is present, not that its stated quantities equal `QUANTITIES`'s. A per-ingredient
  string/number match between the two sections is mechanical, though it would need a defined parsing
  convention for both sections (ingredient name + amount) to compare against reliably - that parsing
  design is the open part, not the comparison itself.

Neither is scoped into v1a's implementation plan; both are candidates for whenever a v1.x follow-up
to the deterministic validator is considered, alongside the existing v2 list above.

## Open decision: small-change carelessness (v2)

Marco's concern is an honest agent carelessly mis-declaring a material change as `small`, not a
malicious one gaming the system. The fix should be a deterministic speed bump, not independent
verification for every `small` change — but its trigger condition, what it actually requires of the
agent, whether it's a hard block or a warning, and where it lives are all undecided. v1a's logging
of real `small`-declared diffs is the intended input for designing this (see Versioning plan,
Logging and observability).
