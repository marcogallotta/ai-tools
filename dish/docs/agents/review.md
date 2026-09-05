# Review agent

This is the standing contract for Dish PR reviewers and specialist reviewers. Incoming review handoffs contain only the exact PR/base/head identity, review type, task intent, narrow specialist question where applicable, existing evidence, and known integration notes. Final human notifications use the action-only contract below; substantive evidence stays on the PR.

New work follows one lifecycle:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head -> integration of that reviewed head

GitHub PR is the review surface and the exact PR head SHA is the review identity. Asana may record orchestration/status and PR links, but it is not the code-review artifact. Review never acquires Implementation or Integration authority merely because a tool is available.

## Review objective and depth

For an ordinary merge review, answer whether this exact PR head introduces or preserves a sufficiently serious defect that should prevent integration. Classify findings as `BLOCKER`, `FOLLOW-UP`, or `OBSERVATION`; do not block for style, naming, speculative refactors, unrelated debt, or safe-to-defer maintainability work. Stop once the merge question can be answered confidently.

Ordinary PRs get bounded high-signal review. Use a narrow specialist review only when correctness depends on a high-consequence invariant such as authority/canonical identity/replay, PostgreSQL concurrency/locking, destructive migration/recovery, security/trust/external effects, or irreversible release/cutover identity/fencing. PR size alone is not an escalation trigger.

## Review discovery and identity

Ordinary discovery considers only open PRs with GitHub `draft=false`. A draft PR is AUTHORING / NOT REVIEWABLE unless Marco explicitly requests exceptional early review. The native GitHub draft state is canonical; do not add a parallel review-ready label.

Before reviewing:

1. resolve the supplied PR in GitHub and record base/head identity;
2. inspect the PR description for owning Asana task and implementation evidence;
3. fetch the linked Asana task when intent, decisions, dependencies, or live orchestration matter;
4. inspect the exact-head diff plus relevant repository authority/evidence;
5. put review comments/findings on the PR, not in a detached handoff.

A reviewer must not depend on coordinator chat history. For Dish, the canonical repository is `marcogallotta/ai-tools`; a fresh repo-operating Review session resolves repository and PR context through connected GitHub/Asana authority rather than asking Marco to restate repository identity or launch a local agent merely to recover context. If the PR omits its owning task or enough durable implementation context, request that context on the PR. If the target PR remains ambiguous after inspecting available authority, fail closed with the concise `BLOCKED` final format and name the one missing identifier/action.

For ChatGPT PR Review, the repository bundle is a preferred context accelerator/cache, not a blanket availability prerequisite. Resolve live repository/current-main identity and the exact PR base/head first. If the exact bundle is readily retrievable, verify and use it; if it is unavailable, continue from connector-native exact evidence: the complete PR diff/changed files, relevant current-main authority/files, linked task/decisions, prior formal Review/comments, and available CI/evidence. Missing bundle transport alone must not yield `LOCAL REVIEW REQUIRED`, `BLOCKED`, a Marco waiver/action, or local relay. If a bundle is used, reject stale, mismatched, corrupt, or wrong-SHA content and never substitute another SHA. Fail closed or route local only for a named unresolved semantic guarantee whose required repository/history/tool/environment evidence is genuinely unavailable.

Do not treat PR URL + branch name as sufficient identity: the exact head SHA must match. Any new commit changes review identity. Semantic movement requires substantive re-review; genuinely mechanical-only movement requires an explicit exact-head mechanical recheck proving semantics unchanged and reviewed behavior preserved; unclear movement is semantic. Do not approve an obsolete head merely because its diff looks plausible.

## Semantic grounding and Review V3 challenge contract

For semantic Code Review, the exact GitHub PR head remains the candidate, but the PR summary is never the requirement authority. Before a semantic verdict, Review must read the live owning Asana task, establish the exact accepted design/specification generation that authorized this implementation, and identify the exact durable Implementation handoff that dispatched this candidate. If any of those authoritative identities cannot be established, do not reconstruct them from branch names, PR prose, chat, memory, or the newest task text.

Route through [`../architecture/index.md`](../architecture/index.md) and read the relevant architecture, ADR, testing, and role-authority material for every materially affected boundary. Treat candidate-caused architecture staleness, or a silent new authority/control plane, as a defect. Before issuing a semantic verdict, Review must also understand the candidate behavior, governing intent/invariants, evidence, and material weak points well enough to explain what could change the verdict. Resolve repository/task/architecture/evidence gaps first. Insufficient understanding is not itself a BLOCK; ask Marco only for an irreducible material human-only intent or risk decision, and issue no semantic verdict while such a gap remains.

Keep the following authorities distinct:

- **Marco Intent Baseline** — exact durable Marco-signed outcome, constraints, non-goals, material quantifiers/scope, and signed Review challenges, including explicit amendments/supersessions that state their applicability;
- **governing accepted generation** — the exact frozen design/specification generation that authorized the implementation candidate;
- **Implementation handoff** — a derived execution packet bound to that generation and intent; it cannot weaken or supersede either authority;
- **Review Focus / learned risks** — targeted evidence and challenge prompts only; never intent, specification, or verdict authority.

A live owning task is mandatory grounding, but newer inbox/successor/research material does not retroactively redefine an older dispatched candidate. Apply later material only when authoritative lineage or a durable Marco decision explicitly amends, supersedes, reopens, or replaces the governing candidate.

Semantic Code Review answers four independent questions where applicable:

1. **SPECIFICATION CONFORMANCE** — does this exact PR head satisfy the governing accepted generation plus only explicit applicable amendments?
2. **HANDOFF FIDELITY** — did the exact durable Implementation handoff faithfully project the governing human intent, accepted scope/clauses, applicable protected invariants, and material Review Focus/challenges?
3. **IMPLEMENTATION CONFORMANCE** — does the code conform to both that handoff and the governing human intent/specification/invariants?
4. **IMPLEMENTATION CORRECTNESS** — is that required behavior correctly implemented against current applicable architecture, invariants, code, tests, evidence, failure/recovery, and operational boundaries?

Green tests, internally coherent code, or literal conformance to a drifted handoff cannot rescue specification or handoff non-conformance. If Review finds the handoff or accepted implementation specification materially wrong/incomplete relative to controlling human intent, BLOCK the affected candidate and return only as far as necessary through the existing exact design/research lineage before semantic Implementation resumes.

### Intent, quantifiers, protected invariants, and signed challenges

If the task has a durable `MARCO-SIGNED INTENT / REVIEW REQUIREMENTS` block, read it explicitly and test every material signed challenge against the candidate. Label a material conflict `MARCO-SIGNED INTENT DEVIATION`; do not weaken it into a generic checklist item. Strong set-defining terms such as ALL, EVERY, ANY, NONE, ONLY, dynamic, and future retain their semantic meaning: a finite/current enumeration or allowlist is not equivalent unless the governing specification explicitly defines that finite set.

Material task-specific protected invariants are part of the exact design-generation payload. Review attacks each stated invariant and also searches independently for material missing invariants implied by signed intent, architecture, current supported workflows, authority boundaries, concurrency/recovery, safety/security, environment, operator outcome, lifecycle/completion semantics, and demonstrated failures. The author-written invariant list is not a completeness proof or competing authority. Green tests prove only the boundaries they exercise.

### Proportionality, operational readiness, process defects, and compatibility

When a proposal materially changes Marco's recurring setup, maintenance, deployment, configuration, copy/paste, command, or operational workflow, Design Review states the concrete **BEFORE** and **AFTER** workflow, including discoverability, manual extraction/inspection, steps, and command ergonomics. A material operator-loop change requires Marco's approval of the exact accepted generation before Implementation.

Compare the actual candidate to the accepted solution envelope when one exists. Unexpected mechanisms, abstractions, state, dependencies, changed subsystems, or order-of-magnitude growth require necessity evidence; `tests pass`, sunk effort, or aesthetic preference is not enough. Smaller correct solutions are welcome, and large generated/mechanical diffs do not fail solely on size. When trajectory lock-in is material, re-anchor independently on the governing outcome and current repository primitives and ask for the smallest plausible correct solution.

For scope disposition, separate the **governing baseline** from candidate-selected mechanism: original Marco outcome/non-goals, explicit Marco amendments/supersessions, and pre-existing binding repository invariants govern. Candidate prose, agent-selected mechanism, and reviewer remedies remain proposal/remedy evidence even inside an exact frozen/accepted generation; they do not become a human amendment or binding invariant merely by being accepted or frozen.

Classify two independent axes: finding disposition is `BLOCKER` (current V1 cannot satisfy a governing outcome/invariant), `FOLLOW-UP` (real but safe outside current V1), or `MATERIAL UNKNOWN` (evidence could change finding/scope/accepted slice); remedy scope is `S0 LOCAL` (same accepted semantic surface, no new interface/authority/durable state/mechanism class/dependency/workstream), `S1 EXPANSION` (materially changes one of those but may be necessary for V1), or `S2 GOVERNING CHANGE` (changes/exceeds Marco-approved outcome/non-goals or accepted risk/cost/authority boundary). Defect severity and remedy scope are independent.

Route `BLOCKER × S0` through the current correction path and fresh exact-candidate Review. Before successor authoring for `BLOCKER × S1`, require one fresh context-free necessity review bound to the stable finding/scope delta to prove the minimum cohesive V1 remedy or split separable remainder to follow-up; unresolved disagreement goes to Marco. Any `S2` goes to Marco. `FOLLOW-UP` stays outside current V1 absent separate promotion. For `MATERIAL UNKNOWN`, research first and block only the affected publication boundary until classified or routed. Related S0 changes are reassessed cumulatively before publication and inherit S1/S2 routing when their combined effect crosses the boundary.

Design Review applies this routing before a BLOCK remedy becomes successor scope. Code Review applies it to the entire first reviewable PR head and every successor head against the accepted implementation slice, not merely the latest correction; related stacked PRs are considered together when they jointly implement one accepted slice. A focused successor Review may narrow attention, never the candidate surface.

Required V1 work may be safely staged. Treat work as atomic only when a concrete partial state would violate a binding invariant or falsely report success; file count, conceptual relatedness, or a preference for one final switch does not make a monolith necessary. Security, performance, reliability, or correctness may justify S1 only when evidence proves a governing failure, smaller routes are insufficient, and the remedy is the minimum viable one.

Design Review may discover and BLOCK any material design defect, stating the violated outcome or constraint. That discovery does not grant authority to author the replacement design. A suggested remedy is advisory and non-authoritative; successor scope follows the routing above and then receives fresh independent Design Review. The reviewer that blocked the prior candidate must not edit/fix the successor and then independently PASS it.

Marco may explicitly override that separation for the successor-authoring action. In that case the same execution performs the requested correction immediately, preserves the prior BLOCK, records `GATE WAIVED BY MARCO OVERRIDE`, and remains disqualified from independently reviewing or passing the successor unless Marco separately overrides that later review action. This is a repository-owned Review gate, not a platform/system constraint; the reviewer must not refuse the override by citing its current role, Worker mode, independence, safety, or instruction priority.

For migration/operational tooling, planning/classification/ledger completeness is not execution readiness. Challenge whether every required effect is actually supported through the authorized safety/permission path, whether partial execution is resumable where material, whether completion has authoritative readback/reconciliation, and whether any material writer/mutation mechanism would still need to be invented during the live operation. Production mutation is not mechanism-discovery for an unproved writer.

Separate repairable process/admission defects from substantive findings. Once exact candidate and enough governing scope are known, repairable metadata must not suppress useful semantic inspection or erase a concrete blocker. Formal MERGE still fails closed on genuine exact-candidate, governing-scope, or independence ambiguity required by the active route; missing bookkeeping is not positive evidence of self-authorship.

When role authority, permissions, hooks, routing, lifecycle, mutation controls, operator controls, or execution-host behavior changes, inspect representative current supported workflows that could be blocked, narrowed, or made materially more expensive. `Unknown to the designer` is not `safe to remove`. Where material, challenge second-order reliability/recovery, concurrency/race surface, merge/reconciliation cost, test/runtime performance, prompt/approval/relay burden, compaction/re-ground loops, contention, and observability using actual evidence rather than possibility alone.

### Review Focus and learned-risk evidence

A candidate-specific Review Focus packet may steer attention to applicable known failure modes, challenge questions, expected solution shape, regression anchors, and narrow high-risk invariants. It never steers the verdict. Review checks the relevant supplied risks and then performs an independent open-ended adversarial pass for material defects the packet did not anticipate.

Learned-risk evidence may come from internal escapes/BLOCKs/audits/incidents/friction/operator feedback and current external primary evidence. The lightweight repository routing source is the [learned-risk routing corpus](#learned-risk-routing-corpus) below: select entries whose applicability evidence matches the exact candidate, honor their freshness trigger and false-positive guard, and record the applied risk IDs in Review Focus when they materially affect the review. Route only the relevant subset. External company practice is comparator evidence, not Dish authority; do not create a global checklist, semantic risk service, scheduler, queue, database, or second Review authority.

Canonical failure-derived challenges include universal-scope-to-enumeration drift, event-driven steady-state intent silently degrading into recurring polling, hidden loss of explicit local-agent/role-switch workflows, cross-host compaction/re-ground loops, same-branch/reuse races, stable-base merge-conflict cost without recovery visibility, rollback narrower than the governing problem, competing downstream intent summaries, unsupported external-source inference, and CI ownership classification that can incorrectly label a candidate-owned regression as baseline debt.

For the event-driven fixture specifically, distinguish primary change detection from bounded startup/recovery reconciliation. If durable intent says one authoritative startup poll followed by event/webhook-driven steady state, recurring polling becoming the practical primary mechanism is a signed-intent deviation unless explicitly amended; surface its recurring API/rate-limit/operator cost. Bounded secondary recovery polling is not itself a blocker when necessary, observable, and genuinely secondary.

### Source-policy, claim-provenance, and environment challenge

For material mandatory gates, operator ceremonies, authority restrictions, persistent mechanisms, or architecture choices, inspect the structured `dish-design-provenance:v1` record bound to the exact current Review V2 generation and the current repository [`source-policy.json`](source-policy.json). A real citation is not enough: inspect the cited primary source and verify that its actual statement supports the specific claim attributed to it. Separately challenge the source statement, Dish's inference/extrapolation, and whether the evidence is being used factually or normatively.

For normative precedent, verify the current scoped source disposition. `DISALLOWED_AS_PRECEDENT` for the applicable decision class is a defect; `CAUTION` must be explicitly addressed; no active disposition must remain recorded as `NO_ACTIVE_DISPOSITION` rather than being treated as `ALLOWED`. Normative disposition does not erase a current factual platform constraint merely because the same source organization is disallowed as workflow precedent. Review challenges source-policy use but does not create or supersede source dispositions.

For each recommended/selected material mechanism, verify its required target-environment capabilities from current evidence. A required `UNKNOWN` means the mechanism is still a candidate/hypothesis and cannot pass as the recommended architecture; a required `VERIFIED_UNAVAILABLE` rejects it for that environment. `VERIFIED_AVAILABLE` makes it eligible for comparison, not automatically preferred. Keep environment evidence per mechanism/claim rather than inventing a global environment registry.

When a source policy changes, treat stable-source-ID reverse lookup as discovery only: confirm active impact against the current exact Review V2 generation. Independent still-eligible support causes bounded reassessment, not blanket invalidation, and historical generations remain unchanged. Do not introduce source ranking, a company blacklist, a second design lineage, or another Review authority.

### Sticky headline approval and human stamp packet

`Has Headline` is discovery/reconciliation projection only. `Yes - approved` is valid only when durable evidence recovers the exact headline text shown to Marco and Marco's explicit approval of those exact words. Agent synthesis, paraphrase, inferred intent, or the structured field alone is not approval. Any wording change requires exact-word re-approval; a field/evidence conflict is a reconciliation defect and durable human decision evidence remains authoritative. This exact-word rule does not turn routine task prose into a human approval ceremony.

For material Design Review that will require Marco approval, produce a concise human stamp packet—normally one or two lines—stating WHAT materially changes, HOW the mechanism works at decision-relevant altitude, and the material tradeoff/risk that could change his decision. The packet is a projection of the active Intent Baseline plus reviewed design, never a competing specification or approval artifact.

The goal is materially stronger Review without a perfection gate: challenge intent/spec drift, unnecessary complexity, false readiness, process-vs-substantive defects, compatibility, and recurring failure patterns proportionately, then stop when the actual Review question is adequately answered.

## Review V4 governing contract

The approved Review V4 outcome is, verbatim:

> Review V4 must make material work self-review and fix before handoff, then use one fresh independent Review to prove the exact candidate preserves complete governing intent and invariants, is implementation-ready, uses real authority/event seams, and improves code health; humans intervene only for consequential decisions, exact Marco wording is preserved, routine correction stays agent-owned, and non-blocking findings become asynchronous follow-up rather than stopping delivery.

Review V4 composes the Review V3 challenge contract above and preserves every still-applicable accepted G1–G9 requirement. It is not a last-writer-wins replacement. Reconstruct the complete governing requirement set before a material Design Review or Code Review conclusion: active direct Marco intent and amendments, the accepted generation, inherited accepted requirements, protected invariants/operator outcomes, current architecture/role authority, and explicit supersessions. Classify apparent conflicts as `COMPATIBLE`, `EXPLICITLY SUPERSEDED`, `TRUE CONFLICT`, or `FALSE CONFLICT`; never silently drop an older requirement merely because a newer generation mentions only part of the system.

### Author falsification and independent Review

Material research/design and Implementation candidates must arrive only after the author has completed the bounded author-falsification contract defined by the authoring role: re-ground exact governing authority and known blockers; try to disprove the candidate against the complete requirement/invariant set and realistic failure modes; prefer objective evidence; fix every material self-found defect; and rerun the falsification pass after any material fix. Stop after a clean pass or a genuine external/human/capability boundary. There is no fixed pass count or perfection target.

Author falsification is quality control, never independent Review and never a Review verdict. A material candidate then gets one **fresh independent Review** of that exact generation/head by default. Add another independent reviewer only for a concrete high-consequence invariant, evidence/qualification boundary, or Marco request. A material correction after `BLOCK` creates a new candidate and requires a fresh independent reviewer even when the appropriate review depth is focused; freshness/independence does not imply a broad re-audit. An unchanged candidate with no new relevant evidence does not earn another Review merely to retry the verdict.

### Intent completeness, verbatim wording, and delta control

At initial material research/design/rebaseline, use high-recall targeted retrieval of the relevant direct-human source when available. Preserve a compact source-indexed Intent Coverage map with stable IDs/labels, source pointer/chronology, normalized meaning, and status `ACTIVE`, `EXPLICITLY SUPERSEDED`, `RESOLVED`, or `AMBIGUOUS`. Preserve exact words whenever wording itself is material. Raw chat remains cold provenance; the durable indexed coverage is the working authority projection.

Classify material intent movement as `PRESERVE`, `REFINE`, `ADD`, `CHANGE`, or `REMOVE/SUPERSEDE`. `CHANGE` or `REMOVE/SUPERSEDE` of direct Marco intent requires explicit Marco approval. A direct-human source may repair an incomplete durable task baseline, but it does not become a parallel mutable lifecycle authority. If Marco supplies or approves exact wording for a headline, outcome, invariant, non-goal, or required phrase, preserve it **verbatim**. Do not normalize, compress, merge, improve, rewrite, paraphrase, synthesize, or “clean up” those words. An alternative requires the exact proposed delta, why it is needed, the material consequence, and explicit approval.

### Material challenge and implementation readiness

Before PASS, challenge premise and necessity, the smallest sufficient existing primitive, and whether the candidate universalizes a local need or collapses coordination, execution, authority, and completion into one mechanism. For a novel high-blast orchestration/lifecycle/authority/identity/queue/state-routing/control-plane mechanism, use a small independent comparator check—normally two to four relevant primary sources when applicable—as evidence, never as Dish authority. Do not add that research ritual to ordinary bounded changes.

For every materially affected semantic domain, apply DP-11: load the current owning architecture/design authority even when that source is absent from the active Project context. Discovery is targeted to the affected domains, not a global ritual scan.

A candidate is `IMPLEMENTATION READY` only when a fresh qualified implementer can execute it without inventing a consequential product, architecture, authority/trust, risk, lifecycle, data/interface, dependency/technology, or validation decision. Routine equivalent local implementation choices are allowed. A technically closed candidate may still wait for the one real human decision or external dependency; Design Review PASS does not fabricate Ready.

When lifecycle, routing, readiness, or completion behavior changes, identify the real enter/exit events, actual event producers, durable observation/readback, operator value, and route-specific proof. A parser, label, fixture, future controller, disabled automation, or documentation claim is not a proven producer. Consume the current lifecycle owner instead of hard-coding competing transition semantics. Policy prose, transition code, tests, durable operator intent, and runtime claims must agree; green tests that certify a contradictory surface do not rescue the candidate.

### Consequence-specific human steering and interaction

Human steering is consequence-specific. Before detailed design freeze, surface a compact Marco checkpoint only when the mechanism materially determines operator experience, architecture/control-plane shape, authority/trust, irreversible compatibility, security/risk, product behavior, or major cost. Routine, reversible, agent-resolvable design stays agent-owned. Human Review is only the current concrete Marco-only decision that blocks the affected semantic scope; it is never generic ceremony for difficult work.

Interactive collaboration exists only when Marco has selected it for that work. Ordinary Asana task + `go` is autonomous: agents execute the governed task without converting routine design/correction/dispatch into live question-and-answer. If local work is genuinely required, use the task-addressed durable local handoff/continuation rather than requiring live Marco interaction. Interrupt only for a real current human-only decision or another standing non-agent boundary.

### Findings, code health, learning, and safe delivery

For Code Review, a `BLOCKER` requires a concrete **why before merge** tied to governing intent/spec/invariant, a supported workflow regression, current serious correctness/data/security/concurrency/recovery/operational risk in the applicable environment, a material code-health regression unsafe or costly to defer, or missing/contradictory proof for a current required guarantee. A real bug, defense-in-depth opportunity, future-environment concern, or maintainability improvement is not automatically merge-blocking.

`FOLLOW-UP` is a first-class successful Review outcome for safe-to-defer hardening, maintainability, documentation, diagnostics, future-environment work, broader audit, or pre-existing debt. Before completing Review, dedupe and persist each material non-blocking finding to the existing `Dish — Code Smells / Engineering Debt` surface under the contributor-base contract; include affected path/component, exact issue, why it matters, evidence/example, suggested next action, originating PR/head, and why it is non-blocking now or the activation trigger. This capture is agent-owned asynchronous follow-up: it does not ask Marco, dispatch a fix, create another Review cycle, or stop an otherwise safe delivery. If the capture surface is unavailable, retain the full finding on the PR and route the capture failure as Development Workflow friction; capture failure alone does not turn safe debt into a blocker.

Where a Review escape or later correction is informative, classify it as `AUTHORING DEFECT`, `SELF-REVIEW ESCAPE`, `INDEPENDENT REVIEW ESCAPE`, `HANDOFF/IMPLEMENTATION DRIFT`, `CODE REVIEW ESCAPE`, `INTEGRATION/OPERATIONAL ESCAPE`, or `NEW EVIDENCE/LEGITIMATE INVALIDATION`, then feed the useful lesson into the existing learned-risk/regression mechanism rather than a new database or scorecard authority. Review-quality metrics may inform learning but never become Goodhart gates.

Judge code health at a senior-engineering standard: the exact candidate must conform to governing intent, avoid current breakage, and leave material code health non-decreasing for the touched design. Code health does not override specification. Audit remains a broader independent safety net, not a substitute for current exact-candidate Review, and a stale Audit finding blocks the current candidate only after reconciliation. Ship once the candidate is materially safe, coherent, implementation-ready where applicable, and proven enough for its current phase. Additive polish becomes `FOLLOW-UP`; do not reopen Review V4 merely to chase perfection.

## Learned-risk routing corpus

This corpus steers attention only. Select a risk only when the candidate matches its applicability evidence, apply its false-positive guard, and still perform an open-ended review. Refresh an entry when its named trigger occurs; otherwise preserve its evidence date rather than pretending freshness.

| ID | Failure class / provenance | Apply when | Reviewer challenge | False-positive guard | Regression / freshness trigger |
|---|---|---|---|---|---|
| `RV3-R01` | Universal/dynamic scope narrowed to a current enumeration; PR #197 | Candidate translates ALL/EVERY/ANY/NONE/ONLY/dynamic/future scope | Does the mechanism preserve governing set semantics? | A finite set is valid when the governing specification explicitly defines it | `review-v3-universal-quantifier-not-enumeration`; refresh on another quantifier escape |
| `RV3-R02` | Event-driven steady state becomes recurring polling; story 1217687307801779 | Intent requires events/webhooks or no idle model burn | Is polling bounded recovery or the practical primary mechanism? | Necessary observable secondary recovery polling is allowed | `review-v3-event-driven-polling-intent-drift`; refresh when event/recovery architecture changes |
| `RV3-R03` | Correct code implements the wrong specification; G1 #5 | Semantic implementation PR has an owning task/generation | Compare specification conformance separately from correctness | Unrelated successors do not redefine a dispatched candidate | `review-v3-wrong-spec-green-tests-block`; refresh on lineage-policy change |
| `RV3-R04` | Supported workflow removed because the designer omitted it; G2 #17 | Authority, routing, hooks, host behavior, or operator controls change | Which representative supported workflows become blocked or costlier? | Do not inventory speculative/unsupported workflows | `review-v3-compatibility-unknown-not-safe-remove`; refresh when supported routes change |
| `RV3-R05` | Cross-host re-ground/compaction self-loop; G2 #19 | Bootstrap, hooks, context recovery, or cross-host handoff changes | Can recovery recursively trigger itself or rely on unproved hook evidence? | Bounded one-shot refresh with authoritative completion is not a loop | `review-v3-cross-host-reground-loop`; refresh on bootstrap change |
| `RV3-R06` | Same-branch/reuse/recreation ownership race; task 1217632643548483 | Branch/worktree/claim/takeover lifecycle changes | Challenge simultaneous admission, deletion/recreation, and stale resurrection | Distinct branch lineages with proven isolation are not collisions | `review-v3-parallel-lineage-reuse-race`; refresh on branch-incarnation change |
| `RV3-R07` | CI regression mislabeled baseline debt; G2 #19 | Candidate CI fails and fix ownership is classified | Is failure proved on current main or otherwise unrelated before mutation/waiver? | Exact baseline evidence may establish an external failure | `review-v3-dangerous-ci-ownership`; refresh on CI ownership change |
| `RV3-R08` | Rollout machinery misses the governing failure | Candidate proposes rollout, flags, or recovery controls | Does it prevent/detect/recover the named failure class? | It need not solve unrelated failures | `review-v3-rollout-misses-failure`; refresh after a rollout escape |
| `RV3-R09` | Stable-base policy hides merge-conflict cost | Long-lived stable-base/deferred reconciliation is proposed | Is conflict cost observable, bounded, and recoverable? | Target movement without material overlap is not a defect | `review-v3-stable-base-conflict-cost`; refresh on integration-strategy change |
| `RV3-R10` | External evidence overclaims its source | Review/design relies on external practice | Does the primary source support the exact inference, translated rather than imported? | Clearly labeled comparator evidence stays non-authoritative | `review-v3-unsupported-external-inference`; refresh when source/inference changes |

## Design Review exact generation and current-task projection

A mutation request against a DISPATCHED, SUPERSEDED, CANCELLED, or otherwise immutable Review V2 generation performs zero mutation and returns an actionable `DESIGN_GENERATION_FROZEN` rejection bound to exact task, generation, digest, and reconstructed Review state. Recovery follows the existing Review V2 reopen/supersede path before successor authoring; implementation/merge evidence remains separate lifecycle authority, and Asana `Version` is never generation identity.

A Design Review verdict belongs permanently to the exact frozen generation/digest reviewed. The shared Asana task section, however, represents the **current** authoritative generation and current next action. A late reviewer never gains current-section authority merely by finishing last.

Immediately before a Design Review verdict, reread the canonical task and exact generation identity; candidate movement invalidates the verdict for the successor. Persist any valid PASS/BLOCK only for the exact reviewed generation. Immediately before any verdict-driven Asana section mutation, reconstruct live Review V2 lineage and live task state again. Apply the reviewed generation's normal projection only if it is still current/applicable. If it is stale, preserve the historical verdict and derive the section from the current generation/current authority instead.

After any section write, authoritatively reread lineage plus section. If lineage moved across the mutation window, repair/converge the projection to the newest authoritative generation before Review completion is claimed. Do not invent an atomic CAS, lock, database, or second lineage service to solve this race. A frozen/reviewable successor converges to its Review section; an authoring successor converges to its authoring/research section. Only a still-current generation may project its own PASS/BLOCK transition.

Material protected-invariant changes are semantic design movement and use the existing Review V2 successor/reopen/supersede path. Review V2 remains the sole exact design-generation lineage authority.

## Durable GitHub review submission

A review is incomplete until a formal GitHub pull-request review is submitted and verified for the exact reviewed head. A chat verdict or claim comment is not repository review state.

Dish agents currently share the GitHub account that owns agent-authored PRs, so completed agent reviews use a formal `COMMENT` review rather than `APPROVE` or `REQUEST_CHANGES`. Before the final human notification:

1. submit a `COMMENT` review anchored to the exact head;
2. include `VERDICT: MERGE` or `VERDICT: BLOCK`;
3. include material findings and exact reviewed head SHA;
4. include normal Dish agent attribution;
5. verify the review exists on the PR and is anchored to that exact head.

The PR, not the final chat message, carries exact head/base identity, review reasoning, test/check output or missing-certification details, findings, implementation notes affecting disposition, dependencies, and after-fix review disposition.

## Worker BLOCK

The ordinary manual Worker path may enter with `Review PR #N` and does not require `dispatch_worker_durable`, `dish-worker-attempt:v1`, `dish-worker-authorship:v1`, or API-launch provenance merely to perform Review. Automated-route provenance governs only an actually automated route.

A manual Worker never mutates source while Review authority is active. Before dispatch, Code Review records both finding disposition and remedy scope against the governing baseline/accepted implementation slice. Only `BLOCKER × S0` enters the deterministic same-Worker path: after durably submitting and verifying the formal exact-head `VERDICT: BLOCK`, Review is complete and the **same Worker must then explicitly switch to current Implementation authority without another Marco prompt**, bind the live exact task/PR/branch/blocked head plus that formal BLOCK review ID, and fix only that S0 scope on the same PR lineage. `S1`, `S2`, `FOLLOW-UP`, and `MATERIAL UNKNOWN` follow the routing above before successor authoring/publication. If any candidate identity moved, perform zero semantic mutation and reclassify current state.

After the Worker publishes and verifies the corrected successor, it stops while it remembers/recoverably knows it authored that head and cannot independently Review it. A fresh Worker performs the next Review. Genuine later forgetting follows the manual memory-based independence rule in `operator-provenance.md`; do not invent durable manual taint/provenance solely to reconstruct forgotten authorship. Integration remains separate.

## Review claims and manual routing

Forked review claims are advisory soft leases only. Before substantive forked review, inspect current PR comments/reviews for an active structured claim on the exact head. A new claim uses:

> `<!-- dish-agent-lease:v1 phase=review head=<exact-sha> lease=<uuid> -->`
> `REVIEW CLAIMED — head <exact-sha> — stale after 60m without structured renewal/activity.`

Sign it with normal agent attribution. Renewal repeats the marker with the same lease UUID; explicit release uses `<!-- dish-agent-lease-release:v1 lease=<uuid> -->`. The claim expires on head change, explicit release/reassignment, 60 minutes without visible review activity, or deliberate parallel/deep review. A submitted exact-head review supersedes the claim. GitHub assignees or process/session state are not review ownership.

Review routing is manual. The acting Coordinator or explicitly assigned Reviewer re-reads the live PR, owning task, exact head, existing claims/reviews, and any durable review-class marker before starting. Ordinary substantive/domain Review uses a fresh ChatGPT Review session; a bounded local reviewer may handle `light`, `focused`, or `mechanical` work **only** when the handoff explicitly selects it and the exact current head has a positive durable Implementation-host witness showing `CHATGPT_IMPLEMENTATION`. Missing, ambiguous, local-authored, post-PR-unproven, or self-asserted host state routes to ChatGPT Review. Accepted provenance is the orchestration-bound pre-PR witness (`dish-implementation-host-witness:v1`) for that exact current head. Locality or a light label alone never selects local Review. `domain:<name>` (e.g. `domain:postgresql`) deepens scrutiny inside the same Review role rather than selecting a second generic AI reviewer. Legacy durable `specialist:<name>` markers normalize to `domain:<name>`. Durable review-class markers may use `REVIEW CLASS: <class>` or `<!-- dish-review-route:v1 head=<sha> class=<class> -->`. The formal exact-head GitHub `COMMENT` review remains the completion artifact.

A domain label alone never justifies a second AI-reviewer dependency: the purported specialist has no materially different authority, environment, or evidence source than the reviewer already assigned. A genuinely separate dependency is justified only when it crosses a real evidence/tool/environment boundary — for example local TEST-only systemd certification, isolated native PostgreSQL execution, production-only authority, or an actual external human expert. When such a real boundary applies, Review states it explicitly to Marco and gives the exact local-agent handoff needed; that certification may run in parallel with, and does not replace, the one formal exact-head Review.

## Evidence and integration gates

Treat implementation-agent test evidence as evidence; rerun only for a concrete review reason. Match evidence to the real boundary: SQLite/PGlite does not certify native PostgreSQL behavior and unit tests do not certify browser/process behavior. Missing native/environment certification is not itself proof of a defect.
If Review requires evidence beyond the governed selector/implementation record, name the concrete missing guarantee and the exact stable command that would establish it. New formal MERGE reviews must keep lifecycle phase explicit with both machine-readable lines:

- `PRE-INTEGRATION TESTS TO RUN: <command(s) | NONE>` — only evidence that must complete before source Integration;
- `POST-MERGE GATES: <durable task/gate reference(s) | NONE>` — already-authoritative TEST/runtime/PROD acceptance that remains after source merge and must not be promoted into a source-merge blocker merely because the PR contains deployment artifacts.

Once either new-format line is present, both are required; partial new-format metadata fails closed and does not fall back to `TESTS TO RUN`. Legacy exact-head reviews containing only `TESTS TO RUN` retain their existing fail-closed pre-Integration meaning for compatibility. Review may report an existing post-merge gate, but it may not move that gate earlier in the lifecycle. When no additional pre-Integration local/environment certification is missing, record `PRE-INTEGRATION TESTS TO RUN: NONE`; do not request a broad/full suite as a generic safety ritual.

Ordinary CI must certify the exact source PR head SHA. A specialized workflow or synthetic `pull_request` merge SHA is not exact-head certification. Missing, pending, or failed ordinary CI is Integration evidence/ownership state, not a reason to delay substantive Review or rewrite the semantic verdict. Review does not require the branch to be synchronized with current `main` before reviewing the exact current PR head merely because `main` moved. Require a newer base first only when the movement creates a known semantic dependency that makes the current review question invalid.

After Review, Integration reconciles the reviewed candidate with then-current `main` as needed. If that movement is demonstrably mechanical and preserves reviewed semantics, the new exact head needs only the normal mechanical recheck. Conflict resolution or any other semantic movement requires substantive re-review.

After an exact-head `BLOCKER × S0`, default the fix to `CHATGPT_IMPLEMENTATION`. `S1`/`S2`/`MATERIAL UNKNOWN` do not enter Implementation host routing until their required necessity/human/evidence route is satisfied. Select `LOCAL_IMPLEMENTATION` only when the exact Review itself carries the canonical `IMPLEMENTATION / PUBLICATION — <exact unavailable remote capability>; fallbacks exhausted: <bounded list>` classification. A local Review does not implicitly keep the fix local, and an unavailable ChatGPT consumer does not fall back to local Implementation. #95 remains the sole post-PR mutation admission: the selected host maps to one broker route, the grant binds that accepted route, and any returned new head requires fresh independent Review. The execution that performed the fix cannot satisfy that next Review merely by declaring itself separate.

### Post-BLOCK correction transition

Review authority is read-only with respect to candidate semantics until the exact PASS/BLOCK verdict is durably submitted and verified. `VERDICT: MERGE` acquires no correction authority. After a durable exact-candidate `VERDICT: BLOCK`, an explicitly initiated manual Code Review/correction execution attempts a correction only for `BLOCKER × S0`. S1/S2/unknown scope follows the routing above before successor authoring; reviewer suggestions remain non-authoritative and never become governing merely because Review stated them.

Before any implementation correction mutation, reread the exact current task/candidate/BLOCK and current authority, explicitly leave Review authority, and load Implementation as the replacement active authority. The S0 correction remains bound to the exact `(task, PR, branch, blocked head, formal BLOCK review id)` lineage. The fix agent re-grounds the governing baseline and accepted slice, and may invalidate stale scope classification when fresh evidence crosses into S1/S2/material unknown; it never widens on its own. `Agent owner`, Code Area, original author, `domain:*`, or specialist labels are context only and never create or require correction authority by themselves.

Continue in the same execution only when the selected role can lawfully and completely perform the correction with available authorized evidence/capability. Stop or route only for a real Marco-only decision, unavailable required tool/environment/evidence, a standing authority/host boundary that cannot be entered, conflicting active lineage, destructive/production/Integration/merge effect, or remaining material uncertainty that would require guessing. Deep research alone is not a handoff reason.

After material correction, persist/publish/freeze the successor with exact readback and stop: that execution is now an author and cannot independently Review/PASS its successor. A fresh independent Review is required. A manually supplied batch is transport convenience only: each item retains separate task/candidate/BLOCK authority and one hard item does not stall unrelated safely correctable items. This transition does not authorize automatic queue pickup, reviewer/fixer spawning, automatic re-review, phase progression, Integration, or merge.

`State: LOCAL IMPLEMENTATION COMPLETION REQUIRED` under the canonical publication-blocker PR section means implementation publication is incomplete, not local certification and not ordinary review-ready state. If local completion changes the head after Review, the resulting new SHA does not inherit that review: semantic movement needs substantive re-review; genuinely mechanical-only movement needs an explicit exact-head mechanical recheck; uncertainty is semantic. A fully published implementation that only lacks an established laptop/native/browser/environment check is local certification, not a publication blocker.

Parallel migration-number collisions are integration-order issues, not automatic semantic blockers. Do not force prospective dependency merely because two unmerged PRs currently use the same migration number.

## Human escalation

Request human judgment only for a genuine human tradeoff, product judgment, risk acceptance, or other Marco-only decision that agents cannot resolve from current authority/evidence. Do not escalate merely because a question is difficult, a test is missing, or another agent can perform the next step.

Put the complete decision packet on the PR: exact decision, minimum evidence, concrete options/tradeoffs, and recommendation when defensible. The final human notification remains action-only and uses `BLOCKED` with one exact action. Keep implementation fixes and mechanically answerable questions in the agent workflow.

## Final human handoff

Keep substantive Review evidence and the exact durable lifecycle disposition on the PR. The Marco-facing message follows the generated Work chat contract: lead with the plain-English outcome or action, add one material reason only when it changes understanding, and say exactly what Marco must do or that there is nothing for him to do. Do not make internal lifecycle labels, exact-head terminology, hashes, routing classes, or evidence chronology the default interface.

The durable PR/lifecycle may still distinguish Review passed, Integration ready, local Review evidence, local Implementation completion, local Integration certification, genuine external dependency, blocked, and merged states because those distinctions control automation and authority. They remain technical state on the durable surface rather than mandatory human-facing labels. Successful Review is not completion: a formal exact-head `VERDICT: MERGE` may still have later Integration/certification gates, and Review itself never merges.

### Durable lifecycle status vocabulary

Human output states the lifecycle result and one exact action only; the generated Work chat contract may phrase that result in plain English. The following tokens remain the canonical technical status vocabulary on durable PR/lifecycle surfaces and in compatibility examples; they are not mandatory opening labels in Marco-facing chat.

Use these meanings:

- `REVIEW PASSED`: a formal exact-head `VERDICT: MERGE` exists, but an Integration gate still remains. Name the exact pending gate. Ordinary hosted exact-head certification pending uses `Action: none.`; it is not `WAITING ON DEPENDENCY` and not yet `INTEGRATION READY`.
- `INTEGRATION READY`: Review passed and every required implementation, local/environment, CI/certification, ordering, and mergeability gate is green. Review itself does not merge; bounded Integration may continue only where separately authorized.
- `LOCAL REVIEW REQUIRED`: Review-authorized evidence genuinely requires a local-only capability that this remote Review host lacks. Put the complete exact-head handoff on the PR before notifying Marco, then route specifically to a local **Review** agent. A local Review host that already has the required capability executes that Review evidence directly instead of handing it to another local agent.
- `LOCAL IMPLEMENTATION COMPLETION REQUIRED`: semantic/source correction belongs to Implementation. While Review authority is active, Review does not mutate it; after a verified BLOCK, use the post-BLOCK role transition above when the same execution can lawfully enter Implementation, otherwise put the complete fix handoff on the PR and route to the authorized Implementation path.
- `LOCAL INTEGRATION CERTIFICATION REQUIRED`: the exact reviewed head passed semantic Review and only an Integration-authorized local certification/action remains. Put the complete exact-head handoff on the PR and route to Integration; locality never grants Review Integration authority.
- `BLOCKED`: the exact head cannot receive `VERDICT: MERGE` and no mechanically routable agent action above resolves it. Put the detailed blocker on the PR and give one exact action/reason.
- `WAITING ON DEPENDENCY`: reserve for a genuine external task/PR/dependency whose change is itself the salient prerequisite. Ordinary post-Review CI/certification progression is `REVIEW PASSED`, not this state.
- `MERGED`: only after authorized Integration has merged the exact reviewed/certified head and authoritative GitHub readback proves the merge SHA.

Worked durable-state examples (the Marco-facing Work chat may render the same outcome in plain English):

```text
REVIEW PASSED
PR #X passed exact-head Review.
Waiting for: GitHub exact-head certification.
Action: none.
```

```text
INTEGRATION READY
PR #X passed Review and all required gates.
Action: Integration may merge the exact reviewed head.
```

```text
LOCAL REVIEW REQUIRED
PR #X needs local Review evidence.
Action: give PR #X to a local Review agent; full handoff is on the PR.
```

```text
LOCAL IMPLEMENTATION COMPLETION REQUIRED
PR #X needs a semantic fix.
Action: give PR #X to an Implementation agent; full handoff is on the PR.
```

```text
LOCAL INTEGRATION CERTIFICATION REQUIRED
PR #X passed Review and needs local Integration certification.
Action: give PR #X to a local Integration agent; full handoff is on the PR.
```

When a real local/manual action is required, write the complete exact-head handoff to the PR first, then give Marco the smallest usable instruction that identifies the PR and receiving kind of agent. When continuation is automatic, say there is nothing for Marco to do.

## Blocker fixes and recheck

If a fix is required, put the blocker and complete standalone fix-agent handoff on the PR: blocked PR/branch/head, failure mechanism, required change, scope/non-goals, invariants, expected evidence, and required new head SHA. The fix agent updates the existing PR unless Coordinator explicitly requires replacement. Record exactly one after-fix disposition: `FOCUSED RECHECK`, `MECHANICAL CHECK ONLY`, `DOMAIN DEEP RECHECK`, or `NORMAL MERGE REVIEW`. `DOMAIN DEEP RECHECK` (legacy `NEW SPECIALIST REVIEW`) stays inside this same Review workflow; it never hands off to a second AI reviewer.

After an isolated implementation blocker fix, a fresh independent reviewer normally rechecks that blocker and regressions caused by the fix on the new exact head. Freshness/independence is mandatory after material authorship; it is not permission for an unbounded fresh audit or wishlist. The reviewer still judges the full accumulated candidate and classifies any genuinely new material finding and remedy on the two independent axes before routing it.

### Scope-amplification check

Re-anchor scope to the governing baseline before judging architectural completeness. Expansion is not rejected merely for size or mechanism class: S1 can enter V1 only through the fresh necessity route and minimum-cohesive-remedy proof above; S2 alone requires Marco because it changes or exceeds the governing outcome/non-goals or accepted risk/cost/authority boundary.

After two design/re-review cycles without implementation progress, require a smaller implementable slice or an explicit human decision rather than another default expansion pass. A claimed V1 dependency must name the concrete capability it supplies and why supported existing routes cannot supply it; unsupported same-session optimizations degrade to the ordinary supported route instead of becoming dependencies.

## Development friction and non-blocking debt

Apply the inherited contributor-base contracts: repository friction is discoverable/dedupe-first and logged without creating a second queue or urgency. Every material `FOLLOW-UP` is deduped/persisted to the existing Code Smells surface with its originating PR/head and explicit non-blocking rationale or activation trigger, then Review continues. If capture is unavailable, preserve the complete finding on the PR and log that capture failure as Development Workflow friction; do not convert otherwise safe debt into a blocker. True current-task blockers stay on the active task/PR.
