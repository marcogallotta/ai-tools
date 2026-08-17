# Dish operator / orchestration control plane

This is the shared presentation and operator/orchestration mechanics contract referenced by the canonical Dish role index. It does not replace the mapped standing role, grant semantic or mutation authority, create a scheduler/queue/ownership service, or change Review/Integration/runtime authority.

All standing roles apply **Shared operator interaction**. Coordinator and Development Workflow additionally apply the task-specific control-plane sections below when performing those actions. Consequential writes still require the current explicit-intent policy and the mapped role's existing authority.

## Shared operator interaction

The generic work-chat behavior is sourced once from `dish/docs/chatgpt-projects/source.json` and generated into root `CLAUDE.md` plus every ChatGPT Project kernel. This document does not redefine that behavior; it adds only orchestration, authority, and readback mechanics.

Conversational execution never creates mutation authority. A current-turn exact mutation request or an accepted contextual follow-up may authorize only the exact previously established scope. If role authority or explicit mutation intent is absent or ambiguous, fail closed at that write boundary. Preserve the host's existing pre-tool/progress-update cadence; a progress message is not completion.

When the mapped role and explicit-intent policy authorize a required orchestration write, perform the smallest write and **authoritative readback** before presenting it as durable. Do not make Marco relay routine state between agents when the workflow already authorizes a durable channel. If the write is not authorized, do not infer permission from substantive input, a task title, a handoff body, or authenticated-account attribution; state the exact missing action instead.

Treat Marco's correction or explicit decision as current input immediately. Re-read affected live authority when state matters and update the current route/presentation within existing authority; do not keep showing stale Decision/Blocked/Ready presentation after the underlying condition has changed. Durable lifecycle classifications remain on their owning authority surface; the human message follows the generated Work chat contract.

## TRUE READY dispatch queue

`Ready` is a dispatchable queue, not a holding area. A task belongs there only when it is dispatchable now: **no unresolved Asana dependency**, **no pending Marco-only decision**, **no required prior design/readiness review**, and **no known active competing implementation lineage**. A stale Ready placement never authorizes dispatch.

Blocked work stays out of Ready. Preserve the real Asana dependency where available and add the smallest stable task-name marker such as `[blocked on <gid>]`. Implementation-ready tasks carry one or a few stable coarse areas using the smallest existing Asana representation, for example `CODE AREA: lifecycle/CI, agent tooling`. Code area is a **first-pass overlap hint only** and **cannot authorize work by itself**; missing/stale metadata never authorizes dispatch.

Coordinator's ordinary next-work path is deliberately narrow:

1. read maintained Ready;
2. compare priority/urgency;
3. compare coarse code areas with current In Progress implementation work;
4. perform **one live sanity check** on the selected candidate's dependencies, human-decision/review gates, and competing GitHub lineage;
5. dispatch only if that live check agrees.

A contradiction discovered during the sanity check is reconciled out of Ready rather than ignored. Do not rebuild merged history or long-note provenance unless maintained state is inconsistent. Ready -> In Progress on actual handoff and post-merge reconciliation remain owned by their existing lifecycle mechanics. This policy creates **no scheduler, second queue, or ownership service**. Do not create a specialist scheduler.

## Authorized Implementation handoff state

When Coordinator or Development Workflow emits an **authorized** Implementation handoff, the handoff and owning Asana lifecycle write are one control-plane operation: move the task from `Ready` to `In Progress`, write one durable handoff record, and read both state and record back before claiming the handoff durable.

The durable record contains task GID, target role, handoff timestamp, handoff source, stable handoff identity, and branch/base plus PR/head when known. A branch still equal to base or no PR immediately after handoff is not evidence that the work was never handed off. Later GitHub branch/PR evidence is reconciled onto the same task/lineage.

At three hours after the durable handoff timestamp, if there is still no associated PR or other authoritative implementation evidence, surface exactly one `STALE HANDOFF — owner status unknown` observation for that handoff identity. The alert is observability only. It never authorizes duplicate dispatch, replacement, or a second writer. Before any ownership change, re-read live Asana + GitHub and reconcile the existing lineage.

`scripts/operator_handoff.py` encodes the write/readback and idempotent staleness predicates. It uses the existing Asana orchestration surface and GitHub lineage; it creates no queue, admission database, ownership service, or new control plane.

## Handoff executability preflight

Prepared handoff text is **DRAFT** until a terminal executability preflight succeeds. Before presenting any handoff as executable, copy-ready, or ready to send, resolve and read back every mandatory durable task/PR/branch/baseline identity, reject unresolved placeholder/template tokens, and verify the receiving standing role required by the role index. A sentence such as `ROLE: Audit` never changes a destination Project/session's standing authority.

If destination authority is unknown, the handoff is routing-required rather than executable. If the destination is known and incompatible, do not emit it as ready for that destination; return the single routing action, for example `send only to an Audit Project/session`. If a prerequisite requires a durable mutation not authorized by the standing explicit-intent policy, stop at `DRAFT / PREPARATION REQUIRED` and state that one missing action rather than creating it implicitly.

Where execution requires a fresh owning task, including an independent Audit round, verify that exact task exists, matches the required scope/baseline, and is distinct from any separate round before the handoff becomes executable. Two independent audit handoffs therefore require two distinct fresh task identities.

`scripts/handoff_preflight.py` is a transport-neutral fail-closed validator for these already-resolved inputs. It never creates a task, changes standing role authority, or performs a prerequisite write.

## Marco decisions versus external blockers

Use `MARCO DECISION — <priority> — <decision>` only when Marco's judgment or approval is the missing authority. Use `BLOCKED — <priority> — <dependency>` for task, PR, environment, external-system, or other mechanically owned dependencies. Deferred future work belongs in Backlog; an actionable investigation belongs in Ready/In Progress rather than being presented as a Marco decision.

Every Marco-decision task begins current notes with a compact Decision Packet bound to a stable decision identity/revision: **Decision needed**, **Recommended answer**, **Alternatives / material tradeoff**, **Consequence of no decision**, and **What happens immediately after approval**. A revised question is a new decision revision and cannot reuse old surfaced state.

Moving a current Marco decision into `Blocked / Decision` creates an immediate surfacing obligation. Startup/status reconciliation also surfaces any current revision not yet durably surfaced. Persist the initial surfaced timestamp; if the same revision is still unresolved after 24 hours, surface it once more and no more. External blockers and prerequisite gates are never promoted into human decisions merely because Marco may eventually approve a later step.

When Marco explicitly answers the exact current decision revision, write that exact answer to the owning task, move it immediately to the correct next lifecycle section, and read both writes back in the same operation flow. A resolved decision must not remain presented in `Blocked / Decision`. Authenticated-account attribution alone never counts as Marco's decision.

For blocked items, keep the wake contract concrete: `BLOCKED ON`, `OWNER OF UNBLOCK`, `UNBLOCK WHEN`, and `THEN`. `scripts/operator_decision.py` encodes the one-shot surface, 24-hour reminder, exact-revision answer binding, and lifecycle readback without creating a human-approval service.

## Research-inclusive development triage

Pending-work triage always covers both implementation-ready and pre-implementation work. After live Asana/GitHub reconciliation, return exactly three operational buckets: **SEND NOW**, **NEEDS RESEARCH**, and **BLOCKED / WAITING**. Research/design/specification work remains distinct from Implementation; an under-specified task never enters SEND NOW merely because it is urgent.

For NEEDS RESEARCH, actively evaluate priority, dependencies, staleness, and whether the research itself is dispatchable now. Dispatchable research follows its normal role/owner without making Marco copy or route agent messages. Critical/high-consequence research is surfaced proactively when it changes Marco's action or decision; lower-priority research remains queued without repeated interruption. Once research resolves the missing design/specification, require the normal durable readiness/review gate before Implementation.

Every triage pass reconciles obvious stale labels before suggesting work: a Ready item already resolved/implemented, a research item whose prerequisite cleared, or a blocked item whose wake predicate is satisfied is corrected to current lifecycle truth.

`scripts/operator_triage.py` supplies the deterministic fail-closed three-bucket projection after those live authority reads. It does not create a scheduler, queue, ownership service, or semantic dispatch authority.
