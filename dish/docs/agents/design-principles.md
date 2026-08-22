# Dish Design Principles

These are the canonical cross-system design principles for Dish development and, where the same invariants apply, Dish product/runtime work. They guide architecture and workflow choices; they do not silently create new operational gates or override narrower standing contracts that own a concrete safety, security, workflow, data, environment, Review, or Integration boundary.

Each principle has a stable ID. The short **Bootstrap** sentence is the canonical concise projection used by generated ChatGPT Project kernels and the generated role-index bootstrap. The detailed text below remains the authority when a projection needs interpretation.

## DP-01 — Parallelize work; serialize authority

**Bootstrap:** Parallel work; serialize authority.

Concurrency is acceptable when overlap can only waste compute, create stale/superseded analysis, or produce competing versioned candidates. Do not require global atomicity, task-wide exclusivity, ownership transfer, or perfect non-duplication merely to prevent duplicated research, Review, testing, investigation, or isolated implementation. Use strong serialization, CAS, fencing, or transactions only where a concrete interleaving can corrupt authoritative state, erase consequential information, violate a protected invariant, or cause an unsafe/irreversible external effect.

Cheap visibility, claims, leases, grabs, and dedupe are useful efficiency mechanisms, but they are not correctness authority unless a narrower contract explicitly says otherwise.

## DP-02 — Automate with visibility and control

**Bootstrap:** Automate with visibility/control.

Prefer executable, repeatable automation over manual ceremony for routine verification, reconciliation, evidence collection, and lifecycle mechanics. High-consequence automation must expose enough state, provenance, and control that Marco can understand what is happening and intervene at the consequential boundary. Automation must not become an opaque authority layer that silently acts underneath the operator.

## DP-03 — Do not invent mandatory gates

**Bootstrap:** No invented mandatory gates.

Agents may recommend rollout, canary, rehearsal, test, activation, or other safeguards, but may not promote a new restriction into standing mandatory policy unless an existing canonical principle or narrower standing authority already authorizes that exact gate, or Marco explicitly approves the tradeoff. If required evidence has no supported executable path, the missing infrastructure is the defect; do not convert that absence into an indefinite manual prerequisite. Existing explicitly authorized safety, security, Review, Integration, environment, irreversible-effect, and human-decision gates remain intact.

## DP-04 — Put human review at consequential decisions

**Bootstrap:** Human review at design/risk, not routine code.

Marco is not the standing line-by-line PR reviewer. Human attention belongs at consequential design, intent, authority, product, security, and risk decisions. Once those choices are authorized, independent agents review the exact implementation head for correctness and fidelity, while CI/Integration/executable evidence verify mechanical behavior and protected invariants. If Implementation or Review discovers a new consequential choice outside the approved design, escalate that delta rather than reopening routine implementation detail.

## DP-05 — Treat human attention as scarce

**Bootstrap:** Human attention is scarce.

Do not make Marco the scheduler, poller, transport operator, standing code reviewer, or resolver of deterministic workflow mechanics when the system can safely perform or reconcile them. Human interaction should carry decisions, exceptions, or genuinely irreducible ambiguity. Missing automation is an engineering problem, not a reason to permanently externalize routine coordination cost to the operator.

## DP-06 — Publication shape is a heuristic, not a ceremony

**Bootstrap:** PR shape heuristic; atomic only for named invariant.

A workstream does not become one indivisible PR merely because tasks share files, architecture, or delegation. Prefer independent or explicitly ordered publication units when intermediate states are safe and separation reduces review/rework/failure coupling. Conversely, land changes together when splitting them would violate a named invariant. Publication granularity is an engineering heuristic, not a new blocking gate: do not manufacture ceremony around a preferred PR size or shape.

## DP-07 — Repository landing is not operational completion

**Bootstrap:** Merge != operational completion.

A reviewed source merge proves only the repository state that was actually reviewed and integrated. Runtime activation, deployment, migration, environment certification, external effects, or other post-merge acceptance remain separate when the owning system requires them. A post-merge requirement must not become a source-merge blocker merely because it appears in the same work item; preserve the actual phase and authority of each gate.

## DP-08 — Prefer exact, versioned, recoverable lineage

**Bootstrap:** Exact/versioned/recoverable lineage; dedupe best-effort.

Bind meaningful outputs to immutable or exact input identity where practical: commit/head SHA, task/design revision, generation, digest, ETag, or equivalent. Stale work is stale in applicability, not automatically worthless; preserve useful provenance and reconcile it against current authority. Consequential human input requires durable recovery provenance so a later mutable projection cannot silently erase it. Replacement or recovered implementation should prefer fresh versioned lineage over unsafe writable takeover of stale/merged state. Cleanup and supersession follow only after the successor is durably established.

## DP-09 — Consequential human decisions are explicit, not irreversible

**Bootstrap:** Marco consequential reversals explicit/durable.

Marco's consequential decisions and constraints are authoritative for their scope until explicitly revised. Agents may challenge or recommend changing them, but must not silently reverse them through implementation or policy drift. A consequential reversal is surfaced and durably recorded as a new decision. Ordinary implementation corrections, code review fixes, and mechanical improvements do not require renewed human approval unless they introduce a new consequential design/authority/risk choice.

## DP-10 — Real-environment certification must be concrete and narrow

**Bootstrap:** Real-host checks only for concrete CI gaps.

Narrow real-machine or installed-environment certification is valid when a concrete authorized boundary genuinely cannot be exercised by repository CI or other supported automation. Name the exact capability and evidence it proves. Do not generalize that exception into blanket manual testing, rollout, canary, or activation gates, and do not claim broader assurance than the real-environment check actually establishes.

## DP-11 — Working context is not the whole design estate

**Bootstrap:** A role/Project is a working-context boundary, not an exhaustive design corpus.

A role/Project is a working-context boundary, not an exhaustive design corpus. Before a material cross-system design, implementation decision, or Review conclusion, identify the affected semantic domains and load their current authoritative design/architecture sources. Absence from the current Project is never evidence that no governing design or invariant exists. Discovery should be targeted to affected domains rather than a ritual global scan.
