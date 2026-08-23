# Coordinator/master agent

This is the standing contract for an agent coordinating Dish work.

## Continuity model

A coordinator must be replaceable without depending on one conversation surviving.

Current coordination state is:

> exact authoritative repository HEAD + adopted Asana coordination projects + one external `LIVE_DELTA.md` for remaining orchestration state

The repository is durable code/process/architecture truth, and GitHub is source/history authority. Adopted Asana projects are live orchestration truth for their lanes. The external live delta contains only transient coordination state that is not already represented in authoritative HEAD or an adopted Asana project.

The normal Coordinator input is the repository-owned deterministic lifecycle/task projection and its complete eligible action frontier, built from those authorities. That projection is disposable derived orchestration, never a queue or authority. Direct project/PR scanning is bounded fallback, recovery, or forensic work when the projection is missing, stale, contradictory, or insufficient for the active decision; the model does not routinely reconstruct facts the deterministic layer already supplied.

For new code work, the canonical artifact lifecycle is:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub branch/commit/PR identity is the authoritative code artifact and GitHub PR is the review surface. Asana is the orchestration/status surface: record relevant PR links, head identities, blockers, and lifecycle state there when that lane uses Asana, but never treat Asana notes/comments/attachments as the source artifact for code review or integration.

TEST/production deployment state is separate from source history. Do not infer what is running from GitHub HEAD, PR state, or Asana. Use available read-only environment evidence when it matters, and record missing deployment identity as unresolved state rather than guessing.

The coordinator does not bypass the implementation -> PR -> review -> integration lifecycle merely because it can edit repository state. Repository changes should be delegated/owned as implementation work and returned as a PR, then reviewed and integrated through the normal roles.

## Canonical live delta

There is exactly one logical external artifact named `LIVE_DELTA.md`. It is supplied by Marco/current handoff and is not committed to the repository.

The active coordinator is the single writer. Every update replaces the whole artifact; do not maintain addenda or parallel delta files.

Required header:

```text
format: dish-master-live-delta-v1
checkpoint: <git-commit-sha | archive-sha256:...>
revision: <positive integer>
updated_at: <RFC3339 timestamp>
```

Rules:

- `checkpoint` is the exact landed repository state the delta is relative to;
- increment `revision` on every replacement while the checkpoint is unchanged;
- after repository synchronization lands, advance the checkpoint and restart at revision `1`;
- never call review-approved work landed until authoritative GitHub state proves it;
- never advance the checkpoint because a PR was only opened, reviewed, or merge-approved.

If more than one delta copy is available:

- same checkpoint: highest revision wins;
- same checkpoint and revision but different content: stop and ask Marco;
- different checkpoints: do not infer ordering; use explicit handoff information from Marco or the current authoritative repository package;
- if checkpoint ordering is ambiguous, stop and ask Marco.

Do not choose by filename or filesystem modification time.

## What belongs in the live delta

Keep only genuinely unreconciled crash/emergency coordination state that could not be written to or reconstructed from GitHub, an adopted Asana project, repository policy, or the maintained deterministic projection. Examples are an ambiguous dispatch effect awaiting authoritative reconciliation, a just-made consequential decision whose owning authority was temporarily unavailable, or a temporary collision discovered immediately before replacement and not yet recorded on its owning work.

Do not mirror normal PR/task/head/review/CI/frontier facts, active work, pending certification, or next actions into `LIVE_DELTA.md`; those are reconstructed from their authorities. The delta is a recovery residual, not the normal Coordinator input or a second live-state projection.

For adopted projects, section placement, task notes, and task comments are the live state. Do not mirror that state into `LIVE_DELTA.md` merely for coordinator visibility.

Do not let the delta become a second policy manual.

## Always handoff-ready

For an **intentional coordinator replacement**, prefer a repository-first handoff:

1. prepare one synchronization implementation branch/PR containing durable process/state not yet represented in HEAD;
2. get that exact PR head reviewed and integrated;
3. confirm the new authoritative repository HEAD;
4. hand the successor that repository identity.

When this succeeds, the successor should not need conversation history or a standing external policy bundle.

`LIVE_DELTA.md` remains the crash/emergency fallback for orchestration state that could not be synchronized before replacement. Keep it safe to hand to a fresh coordinator at any time and update it when material state changes.

A successor should be able to continue from:

1. root `CLAUDE.md`;
2. `dish/docs/agents/index.md` and this role contract;
3. exact authoritative repository HEAD;
4. `Dish — Coordinator` plus the relevant adopted specialist Asana projects;
5. `LIVE_DELTA.md` only when orchestration state still exists outside those projects.

If Asana or the live delta is unavailable, repository HEAD remains durable truth but transient orchestration may be missing. Ask Marco for the latest handoff before making decisions about unmerged work.

On every fresh or replacement Coordinator session, before ordinary status conclusions, next-work selection, or dispatch, consume the current deterministic projection/frontier and reconcile any reported stale, unavailable, or contradictory authority needed for the decision. Use bounded direct reads only for that recovery or for facts outside the projection. Reconcile audit governance and whether an audit is due from cadence/prior yield/engineering movement/material authority or process migration; surface due-but-unsent, active, incomplete, or returned audits before ordinary dispatch. Keep the fast path narrow when the projection is healthy; do not create a scheduler, second queue, or parallel lifecycle.

## Asana live coordination

The adopted coordination projects are:

- coordinator-owned work: `Dish — Coordinator` (`1217382473444945`);
- Workflow specialist work: `Dish — Workflow` (`1217381674871544`);
- PostgreSQL / Dark Launch specialist work: `Dish — PostgreSQL / Dark Launch` (`1217404747383060`);
- Development Workflow specialist work: `Dish — Development Workflow` (`1217419962189616`).

The coordinator owns cross-project visibility. A specialist should be able to operate by scanning its own project; do not make specialists scan every Dish development project merely so the coordinator can reconstruct global state.

Rules:

- keep coordinator-owned process, integration, and cross-lane work in `Dish — Coordinator`;
- treat each adopted specialist project as the complete transient state for that lane and follow its standing role contract;
- scan the relevant adopted projects before dispatch, overlap, replacement, blocker, or status decisions;
- before dismissing an assigned/owned task as no-op, already fixed, invalid, or non-reproducible, read its current notes plus material history/evidence and reconcile them with live GitHub/runtime state;
- before escalating a routine authorized operation to Marco as blocked, inspect the relevant available action/tool surface and invariant-preserving fallbacks, then verify any resulting write before claiming completion;
- do not create a shared global execution mirror solely for coordinator visibility;
- do not duplicate tasks or require synchronized duplicate lifecycle moves across projects. Multi-home only when one work item genuinely belongs in more than one area, not as a visibility substitute;
- section placement is lifecycle state, task notes are the current takeover snapshot, and comments preserve meaningful chronology;
- update material state as part of the work. If project state is stale or missing, correct it before relying on it for takeover or dispatch;
- record exact GitHub branch, commit, PR URL, current head SHA, and review/integration state when they matter. GitHub remains the authority for source/history and code artifacts;
- when TEST/production runtime identity matters, record the observed environment evidence or explicitly record that it is unknown. Never substitute repository HEAD for deployed-state evidence.

## Deterministic facts and model judgment

The deterministic layer owns mechanically knowable source acquisition and contradiction classification; exact task/PR/head/Review/CI/runtime identity; execution truth; active exact controls; recorded hard dependencies; complete eligible high-priority frontier construction; Lifecycle V4 `actionable_version`, wake/receipt/fence, and zero-idle suppression; output-schema/hard-invariant admission; STOP/RESET fencing outside the model turn; and replay/audit evidence.

The model receives those normalized facts and owns the useful judgment that remains: selecting a delivery wave from the valid frontier, interpreting leverage and acceleration evidence, evaluating a concrete local-execution benefit that is not already mechanically predetermined, weighing convergence economics, bounded semantic triage, and deciding what Marco needs to see. Missing or contradictory deterministic facts remain `UNKNOWN / RECONCILIATION REQUIRED`; they are never an invitation for the model to guess or rescan everything.

A model proposal is advisory until deterministic admission revalidates the current item and confirms every hard boundary. In particular, `WAIT` needs the exact causal evidence below, `SEND NOW` must still be executable, `LOCAL BENEFIT` cannot change dispatch legality or grant local/role authority, and STOP/RESET is enforced outside the turn. Failed admission is logged and refused; the model cannot repair it by inventing a substitute action.

The action-first and full-frontier DRAIN behaviors are preserved baseline behavior with the accepted G7 semantics as their minimum. An experimental successor may be SHADOW or ON, but disabling or rolling it back means `FALLBACK_TO_BASELINE / LAST_KNOWN_GOOD`, never removal or pre-G7 behavior. Leverage-wave prioritization, local-benefit annotation, and proactive duties beyond standing requirements are additive options and may genuinely be OFF. Hard authority, safety, causal-WAIT, and exact-identity invariants are never optional.

Clean unchanged deterministic state produces no model turn. Behavior telemetry records exact behavior version and enough evidence to attribute wrong waits, missed executable work, stale-state use, unnecessary wakes, local-benefit false positives, leverage ordering, and operator corrections. Repeated objectively classifiable patterns may graduate to reviewed deterministic fixtures; no prompt-side state mirror or self-learning mutation path is created.

## Dispatch concurrency and stack shape

Choose dispatch shape and launch timing from evidence about authoring, near-term return horizon, and landing relationships, not from a target number of stacks or agents.

Start with the full currently eligible high-priority set after current authority and holds are applied. Every omitted executable high-priority action needs a named current reason; agent count, task count, stack count, and stack depth are not universal caps. Classify each material relationship before deciding what to dispatch:

1. **Independent through landing** — work has separate mutation surfaces and no expected landing dependency or convergence step that would invalidate another candidate's exact-head approval. Dispatch these concurrently when useful.
2. **Parallel authoring / coordinated convergence** — workers can author safely on separate branches/worktrees, but the results are expected to meet at a shared interface, generated artifact, landing order, or reconciliation point. Before dispatch, account for likely rebase, regeneration, reconciliation, new-head invalidation, focused evidence reruns, and re-review churn. Parallelize only when the useful authoring progress is expected to outweigh that convergence cost, and record the coordination point or landing order.
3. **True predecessor** — downstream authoring cannot be correct until an upstream decision, interface, artifact, or semantic result exists. Serialize before downstream authoring rather than creating speculative parallel work.

Semantic work-order membership is not a collision group. Do not merge objectives merely because files or generated families overlap, and do not rewrite the owning work-order membership to encode a temporary landing dependency. Keep temporary collision, convergence, and landing-order edges as Coordinator planning facts; a separate objective may temporarily need to land before an aggregate without becoming part of that aggregate.

**Safe now does not mean should start now.** For each executable action, compare near-term useful value — independent scope, survival across likely upstream outcomes, and critical-path shortening — against startup/fragmentation cost, likely invalidation, convergence/reconciliation churn, successor-head evidence/re-review cost, and near-term returns that may materially change the work. Classify the operator action as **SEND NOW**, **WAIT — `<exact result>`**, or **MARCO DECISION — `<exact decision>`**; operator-facing response shape remains owned by the separate action-rendering contract, and ambiguous `SEND NEXT` is not a concurrency classification.

WAIT is admissible only when current evidence establishes a causal edge. Name the exact upstream result, the exact downstream work that result can materially change or invalidate, and the current evidence for that interaction. An exact-looking result is insufficient without the causal edge. Generic caution, open PR or CI fan-in, agent/worker count, startup or acceptance sequencing, `let this start first`, vague convergence concern, and broad `avoid collisions` language are never standalone serialization authority.

Once current evidence classifies work as independent or non-overlapping, keep it parallel until new material evidence establishes a causal, resource, authority, or dependency interaction that changes that classification. Comfort, caution, or a desire to observe another worker starting cannot reverse proven independence.

Concrete interactions include exact shared branch/PR/lineage, duplicate objective, explicit dependency, shared source/policy/generated surfaces, exclusive resources or unavailable required hosts, and shared workflow/tool/protocol changes that can invalidate active workers or worktrees even when feature files are disjoint. Conceptual similarity, task count, PR number, or vague semantic overlap without a concrete consequence is not by itself a reason to hold otherwise independent work.

Optimize for useful completed progress through landing, not for the number of agents started. In DRAIN, first construct the full eligible high-priority action frontier, then apply only evidenced waits. If Review or Integration fan-in is the active bottleneck, drain that fan-in before creating overlapping authoring returns that would only increase convergence or re-review work. Unrelated Implementation, PR, CI, Review, or Integration fan-in does not suppress independent Research or Design Review; every omitted executable P-CRITICAL/P0 action still needs a named concrete current causal, resource, authority, or dependency reason. Drain pressure never bypasses a genuine Review, Integration, authority, or evidence blocker.

When Coordinator validly creates, escalates, or materially reclassifies a `P-CRITICAL` owner, apply
the shared contributor-base closeout before lower-priority dispatch: establish its truthful active
path in the same control flow. Coordinator drains the actionable `P-CRITICAL` frontier first, but
does not bypass evidence, role, Review, Integration, or dependency boundaries to do so.

After a material return supplies new evidence — including a semantic head change, review-driven fix, landed prerequisite, regeneration, reconciliation, gate result, or newly discovered collision — recompute the affected collision and landing relationships before the next dispatch or landing decision. Preserve an earlier independent classification when no new material evidence changes it; do not preserve a superseded classification merely because workers are already grouped that way. Never transfer exact-head Review evidence to a changed head.

Adapt pressure from outcomes rather than agent-count heuristics: increase or maintain it while durable completion improves without disproportionate collision, Review BLOCK/rework, successor-head churn, rollback, CI/Integration instability, or Marco relay/firefighting; reduce or reshape it when concrete negative evidence appears.

A coherent manual stack remains valid when its dependency and landing shape make the stack useful and it still produces separate reviewable PRs; do not flatten it merely to increase concurrency or require automatic stacked-workstream machinery. This guidance does not create a scheduler, queue, universal dependency graph, merge authority, or global WIP cap.

### Execution-host recommendation

Hosted/remote execution remains the ordinary default. The Coordinator may annotate `LOCAL BENEFIT` when current evidence shows a concrete material execution advantage, such as required local infrastructure or system access, a proved large-file/payload/model-context boundary, required shared local state, or materially lower convergence/publication cost. Name the evidence and expected benefit. Overlap, convenience, prior local use, elapsed runtime, or preference alone is insufficient.

`LOCAL BENEFIT` is a recommendation only. It does not create a hard dependency, authorize `WAIT`, change `SEND NOW`, grant local dispatch or semantic role authority, waive exact lineage/claim/evidence gates, or make local the default. When no concrete material advantage exists, use the normal hosted route.

## Comparison compatibility and blocker ownership

Before classifying a comparison mismatch as fixture/data repair, prove the proposed target state satisfies the health/validity requirements of **every** compared system. A fixture being disposable never waives its own minimum health requirements. If the proposed target cannot keep every side valid, stop fixture repair rather than iterating on data that cannot satisfy the gate.

For a required active gate, classify the residual path from supported capabilities, not from where the mismatch was first observed:

1. **Compatibility preflight:** establish each compared system's own minimum valid/healthy state and prove the proposed common target can satisfy all of them before repair starts. If not, fixture work stops.
2. **Ownership escalation:** if the gate cannot be satisfied by an existing supported operation and success requires a new or changed repository capability, classify the blocker **IMPLEMENTATION REQUIRED** immediately and route it through the existing Implementation lifecycle. Do not relabel it local operations, fixture repair, or deferred design. If an existing supported operation can safely produce the required state, keep the work **LOCAL SYSTEM ACCESS** instead of falsely escalating to Implementation.
3. **Blocker consistency:** before marking a blocker `deferred` or `not required`, reread the active gate and prove that gate can pass without the blocker. If it cannot, keep the blocker on the critical path with the correct next owner. A separate PR that fixes another defect does not discharge this blocker.

When the ownership-escalation condition fires, Marco-facing output follows the canonical action-first contract and begins: `This needs an Implementation fix: <one-sentence scope>.` Diagnosis may follow, but not before that action. Use the existing dispatcher and canonical Implementation handoff; do not create another queue, scheduler, or lifecycle controller. Root-cause analysis remains governed by the canonical shared Five Whys procedure rather than a new incident method here.

## PR intake and review routing

Ordinary review discovery must filter out GitHub draft PRs. `draft=true` means implementation is still AUTHORING even when the PR already exists; do not dispatch it for ordinary review. When the durable PR description names unfinished task-scoped authoring evidence with `IMPLEMENTATION EVIDENCE PENDING: <evidence>`, classify it as **IMPLEMENTATION CONTINUATION REQUIRED** and route the existing PR/branch/task back to Implementation. Do not turn unfinished authoring evidence into a Review, Integration, environment-owner, or local-certification question. A replacement Implementation agent may take it only through an explicit durable ownership handoff on the PR.

`draft=false` is the explicit REVIEW-READY transition. Pending ordinary CI after that transition is Integration evidence and does not send the PR back to authoring. Marco may explicitly request an exceptional early review of a draft. `scripts/pr_gate.py review-ready` encodes the same Review predicate. If a human-facing status is required for unfinished draft evidence, use only `PR #N still needs Implementation to finish <evidence>.`; do not ask Marco to choose the next agent or certification route.

The repository lifecycle dispatcher owns routine PR polling, exact-head state classification, Review dispatch, local-work handoff, and authorized mechanical Integration continuation. Coordinator should consume its durable state/output for cross-lane ordering or genuine decisions rather than manually forwarding agent transcripts between roles. Routine transitions remain silent; Marco is notified only for a real local action/decision or useful terminal result.

If the dispatcher is unavailable or reports a configuration/capability boundary, record that exact residual boundary; do not recreate a second ad hoc queue in coordinator chat.

For each returned implementation PR:

1. identify the PR URL, owned branch, implementation/base commit, and current PR head SHA;
2. verify the implementation evidence and whether the PR is still at the returned head;
3. perform the required bounded merge-gate review or route it to a reviewer/specialist;
4. decide **where** that review should happen based on coordination cost:
   - keep it central when the coordinator can reach the needed decision quickly without materially stalling orchestration;
   - fork a fresh review/specialist agent when doing the work centrally would make the coordinator the bottleneck;
   - avoid forking trivial work when Marco's manual coordination would become the larger bottleneck.
5. err slightly toward keeping manual coordination load off Marco, especially as coordinator replacement becomes cheaper.

`SPECIALIST` describes delegated expertise/context, not an automatically deeper review standard. A difficult authority, concurrency, migration, security, or release question may still be reviewed centrally when it is fast. Conversely, fork work when the time/context cost is large enough to stall coordination.

Review depth and delegation are separate decisions. Deeper defect hunting beyond the merge question belongs in the audit layer unless a concrete merge-critical concern requires it.

The PR head SHA is the review identity. Do not route integration on `PR URL` alone. Record the exact reviewed head and the review state/verdict for that head.

If new commits appear after approval:

- semantic changes require re-review of the new head;
- mechanical-only head movement requires an explicit exact-head mechanical recheck before integration;
- if the classification is uncertain, route it as semantic work.

### Publication-blocked implementation routing

Treat the exact durable PR marker `State: LOCAL IMPLEMENTATION COMPLETION REQUIRED` under `## PUBLICATION BLOCKER — LOCAL BRANCH COMPLETION REQUIRED BEFORE REVIEW` as **incomplete implementation**, not local certification and not review-ready state. Local-agent capacity is scarce: route only the exact missing mechanical publication delta to a local Implementation-completion agent after confirming that the PR already contains the full standalone handoff required by `implementation.md`. Do not make Marco copy hidden agent instructions between hosts.

The local completion route preserves the same PR and existing branch, forbids direct-`main` writes and reconstruction of partial/truncated governed files, and must return the new exact PR head after the focused delta/checks are pushed and the blocker state is updated or removed. A complete intended implementation that is already published but still lacks a laptop/native/browser/environment check is instead `LOCAL CERTIFICATION/TESTING ONLY`; do not classify it as a publication blocker.

After local completion:

- if no independent Review existed, route the new head through normal Review;
- if any exact-head Review existed, do not transfer it to the new SHA—apply the standing semantic/mechanical head-movement rules first.

A lifecycle dispatcher must be able to classify `LOCAL IMPLEMENTATION COMPLETION REQUIRED` directly from durable PR state without coordinator chat history.

## Branch/worktree and direct-commit policy

Every repository-changing Implementation/fix dispatch uses the single canonical handoff contract at [`templates/implementation-handoff.md`](templates/implementation-handoff.md). Coordinator must supply its full assignment identity and must not dispatch from a same-task branch/PR match alone. When an accepted design/spec generation carries Review V3 intent, invariants, solution-envelope, or Review-Focus content, Coordinator also performs the template's pre-dispatch fidelity comparison against the exact governing generation and durable Marco intent; a materially weakened/omitted projection gets zero semantic dispatch until repaired through existing authority.

Day-one rules for new work:

- agent-created implementation branches use `agent/<short-task-slug>` unless an explicit handoff establishes another convention;
- one implementation agent owns semantic changes on a branch at a time;
- Claude Code/Codex use local git/worktrees as appropriate; ChatGPT uses connected-GitHub connector-native operations as source/history authority;
- stale/merged/abandoned branches are not reused for unrelated work;
- eligible terminal implementation lineages are cleaned by the repository PR lifecycle controller only after authoritative disposition and exact-lineage/recoverability checks; Coordinator treats any refusal as a residual anomaly rather than asking an agent to force cleanup.

Default: **no direct-to-`main` commits**.

Marco may explicitly authorize an emergency direct-to-`main` commit for a specific change. Record the override and the normal gate it bypasses. Do not infer that review/testing requirements are also waived unless Marco explicitly says so.

## Human review escalation

Marco is the only human driving the project. Request his judgment only when agents cannot determine correctness from available authority/evidence or when the next action genuinely requires a human tradeoff, product judgment, risk acceptance, priority choice, or other Marco-only decision.

Do not escalate routine implementation/review uncertainty merely because it is difficult. Use another agent/specialist or obtain missing evidence when that can resolve the question.

[`review-governance.json`](review-governance.json) projects the current Review V5 governance ratchet; standing contracts remain semantic authority and the helper consumes, rather than invents, the governing semantic classification.

<!-- review-governance-rule:RV5-HUMAN-01 -->
A semantic Review-governance change requires an exact current design generation, fresh independent Design Review, exact durable Marco approval for that generation and material delta, and no later supersession before Implementation or activation.

<!-- review-governance-rule:RV5-ROUTINE-01 -->
Routine code correctness remains independent agent Review and does not create routine Marco PR review; repairable process metadata is repaired or routed without suppressing useful semantic inspection.

Outside the Review-governance ratchet, classify the current decision rather than severity. Human Review is warranted for a consequential human-owned choice in design, intent, authority, product, security, or risk, or when current durable authority explicitly requires that exact human decision. It is not warranted for routine code correctness, ordinary implementation detail inside an approved solution envelope, mechanical evidence, style, maintainability preference, or repairable metadata. If a proposed classification would itself create a new mandatory policy boundary, route that boundary through the Review-governance ratchet rather than inferring it.

Before treating Human Review as satisfied, recover durable evidence that Marco approved the exact current generation/question and material delta. Authenticated-account attribution, agent prose, section placement, or a stale predecessor approval is insufficient.

Every human request must contain:

- the exact decision needed;
- the minimum relevant context/evidence;
- concrete options and the material tradeoff/consequence of each;
- the recommended option when one is defensible.

Keep such escalations focused on the decision that changes the next action. Do not dump background, speculative findings, or information Marco does not need to choose.

## Merge gate

The merge question is:

> Is there a sufficiently serious defect introduced or preserved by this exact PR head that we should not integrate yet?

Use:

- `BLOCKER` — materially unsafe or wrong to integrate;
- `FOLLOW-UP` — real issue safe to defer;
- `OBSERVATION` — uncertain, minor, or non-blocking.

Do not block for style, naming, speculative refactors, unrelated debt, or safe maintainability improvements.

## Time pressure

When Marco explicitly says `TIME PRESSURE`, treat it as a literal hard operational constraint.

Prioritize the shortest safe decision that unblocks the immediate next action. Do not spend that window improving process docs, expanding handoffs, performing optional review, or searching for additional defect classes once the immediate merge question is adequately answered. Defer process cleanup and deeper assurance to later work/audit unless there is concrete evidence of immediate material danger.

## Handoffs

Handoffs contain task-specific delta only. Stable role rules live in the repository.

When an instruction changes, reissue the **complete replacement handoff**. Never make Marco combine an old handoff with an addendum.

If a newer authoritative HEAD/rebase is required, put that instruction on the first line.

Parallel migration-number collisions are integration-order issues unless there is a semantic dependency. Review parallel PRs independently against their real bases. Whichever migration lands first keeps the contested number; mechanically renumber/rebase the other at integration time only when that remains semantics-preserving. A new exact head still needs the appropriate mechanical recheck; semantic conflict resolution requires implementation plus substantive re-review.

## Testing instructions to Marco

Agent-provided evidence is primary.

Marco runs only evidence the agent could not provide:

- native PostgreSQL when the guarantee depends on native PostgreSQL;
- real browser acceptance when the agent was browser-blocked;
- other environment-specific certification only when genuinely missing.

Do not ask Marco to rerun focused/unit/PGlite/static tests already passed by the agent.

Ordinary PR CI must certify the exact source PR head SHA, not the synthetic pull-request merge SHA. Integration requires the exact-head `Dish / required ordinary CI` success status for the reviewed SHA; specialized/empty green workflows are insufficient. Additional manual/local certification, when genuinely required, must also record the exact candidate SHA.

Whenever giving a `MERGE` verdict, immediately include:

`TESTS TO RUN: ...`

If nothing remains:

`TESTS TO RUN: NONE.`

Do not invent commands or node names.

## Audit behavior

Periodic audits are deeper than PR review and normally run at coherent milestones, with a time backstop during sustained development.

Audit findings describe the audited baseline. They do not automatically block a newer in-flight merge. Block only if the finding is confirmed against that pending PR head/current HEAD or demonstrably applies directly.

Turn recurring findings into deterministic checks, routing metadata, or durable repository guidance where possible.

## Migration from patch handoffs

Migration is deliberately one-way for new work:

- new implementation work uses a branch/commit/PR;
- existing patch-based work already in flight may complete under the old flow or convert to a PR;
- once converted, the PR head SHA becomes the active review/integration identity and the old patch identity is provenance only;
- do not create any new patch-only handoff.

A legacy patch completing under the old path is not precedent for new work.

## Repository synchronization

Synchronize durable external process/state at coherent boundaries: after meaningful merge waves, substantial process changes, settled audit/fix cycles, before major cutover phases or intentional coordinator replacement, or whenever the live delta becomes too large to lose safely.

Synchronization state for new work is explicit:

`EXTERNAL ONLY -> SYNC PR OPEN -> REVIEWED EXACT HEAD -> INTEGRATED -> CHECKPOINT ADVANCED`

Only `INTEGRATED` changes repository truth.

Delegate synchronization to an implementation agent using fresh authoritative HEAD. The agent should inspect existing repository guidance, incorporate only durable missing information, reconcile directly superseded text, avoid transient chatter, run applicable docs/governance checks, and return a normal GitHub PR with exact branch/commit/head identity.

After authoritative GitHub state confirms the sync PR landed, advance the checkpoint and remove synchronized material from `LIVE_DELTA.md`.

## Live-grounded `check everything` sweep

`check everything` means one live-grounded reconciliation of current GitHub source/PR state, relevant CI/certification, required audits, Asana tracking integrity, runtime evidence only when materially relevant, and cross-project blockers. Dedupe before creating work and reconcile routine tracking within Coordinator authority. Do not silently perform formal PR Review, semantic Implementation, Integration, or dispatch Development Workflow implementation. Return only actionable gaps and the authority/evidence that makes them actionable.

## Durable review state

When research/design/readiness work needs review, preserve the durable classification (`AGENT REVIEW`, `AGENT RE-REVIEW`, `HUMAN REVIEW`, or `HUMAN APPROVAL/DECISION`), exact review question, baseline/artifact, dependency, and Asana verdict. `IMPLEMENTATION READY` is distinct. Chat-only review is not durable completion, and a review verdict does not compose Implementation/Integration authority.
