# Dish agent role index

This is the canonical router for standing Dish agent roles. Root `CLAUDE.md` points named-role work here so role discovery does not require hard-coded routing in multiple files.

All repository-modifying roles inherit [`contributor-base.md`](contributor-base.md). Specialist contracts add their own scope and authority rules.

Every role or Worker mode performing a governed write in the canonical Development Workflow Asana
project also applies the shared [`Development Workflow Asana project mode`](development-workflow-asana-mode.md)
contract before that mutation. This cross-role mutation guard does not compose semantic role authority.

Every other Dish-prefixed Asana project follows the shared, project-agnostic
[`Asana V2 project mode`](asana-v2-project-mode.md) registry instead: it does not replace the
Development Workflow contract above, and a project absent from its registry gets zero governed V2
mutation.

All roles also apply the shared [`Dish operator / orchestration control plane`](../../../OPERATOR_CONTROL_PLANE.md) for presentation mechanics. Coordinator and Development Workflow additionally apply its action-specific queue/handoff/decision/triage sections; that shared file is a decomposition aid, not role composition or a new authority layer.

| Role / common names | Standing contract |
|---|---|
| Coordinator, master, orchestration coordinator | [`coordinator.md`](coordinator.md) |
| Development Workflow specialist, development workflow agent, developer-process specialist | [`development-workflow.md`](development-workflow.md) |
| Audit agent, audit specialist | [`audit.md`](audit.md) |
| Implementation agent, fix agent | [`implementation.md`](implementation.md) |
| Integration agent, integrator | [`integration.md`](integration.md) |
| PR reviewer, review specialist | [`review.md`](review.md) |
| Workflow specialist, workflow agent | [`workflow.md`](workflow.md) |
| PostgreSQL specialist, dark-launch specialist, dark-launch agent, PostgreSQL agent | [`postgresql-dark-launch.md`](postgresql-dark-launch.md) |

## Shared human-facing workflow rules

Anything shown directly to Marco must use plain English. State what happened, what is happening, what is blocked, and what happens next without relying on internal codenames, task-family shorthand, implementation-phase labels, or repository/process jargon. Technical identifiers such as task IDs, branch names, PR numbers, and exact SHAs may be shown when useful, but pair them with their plain-language meaning. Internal agent-to-agent and machine/audit records may stay technical unless Marco asks to see the internal form.

Marco's explicit scoped override is authoritative over Dish process/workflow/test/review/Integration gates for the named operation. If the active blocked gate is already unambiguous, a clear follow-up such as `override`, `go`, `do not run tests`, or `mark in PR override` applies to that gate without requiring special syntax or another confirmation. Execute the scoped override first and record its provenance second. Preserve factual evidence exactly: a failed or blocked test remains failed or blocked, while the lifecycle record separately states `GATE WAIVED BY MARCO OVERRIDE`. Do not infer unrelated waivers. Genuine platform/system constraints remain outside this policy.

<!-- BEGIN GENERATED DESIGN PRINCIPLES BOOTSTRAP -->
## Critical Design Principles

Generated projection of [`design-principles.md`](design-principles.md); canonical detail remains in that document.

Design Principles (design-principles.md): DP-01 Parallel work; serialize authority; DP-02 Automate with visibility/control; DP-03 No invented mandatory gates; DP-04 Human review at design/risk, not routine code; DP-05 Human attention is scarce; DP-06 PR shape heuristic; atomic only for named invariant; DP-07 Merge != operational completion; DP-08 Exact/versioned/recoverable lineage; dedupe best-effort; DP-09 Marco consequential reversals explicit/durable; DP-10 Real-host checks only for concrete CI gaps.
<!-- END GENERATED DESIGN PRINCIPLES BOOTSTRAP -->

## Shared analysis methods

For any requested Five Whys / root-cause Five Whys analysis, read and follow [`five-whys.md`](five-whys.md) before presenting conclusions. The shared procedure is an analysis method only and does not change role authority.

## ChatGPT Project kernels

Recurring ChatGPT role Projects use the concise, versioned kernels in [`../chatgpt-projects/`](../chatgpt-projects/README.md). Regardless of installed Project vintage, fetch this role's current generated Project kernel from that directory on current `main` at the first substantive action and read it as current session policy; installed Project custom-instruction text is a bootstrap/version witness only until this fetch succeeds. Those generated kernels bootstrap high-consequence gates and drift detection; this index and the mapped standing contracts remain detailed role authority. A canonical-version mismatch is non-blocking by itself: fold exact role/action history, continue for `DRIFT 1/3` compatible/unrelated and `DRIFT 2/3` additive changes, and stop/resynchronize only for proof-backed applicable `DRIFT 3/3` incompatibility. Missing, malformed, or unproved drift metadata is `INTEGRITY ERROR · DRIFT ?/3` and fails closed only the affected action for repository repair, not Project resync.

## Shared repository lifecycle

For unqualified Dish PR/issue references, use [`repository-routing.md`](repository-routing.md) when the trigger applies.


Ratified cross-Project standing invariants that must survive Project regeneration/reconciliation are governed by [`standing-invariants.md`](standing-invariants.md) and its independent machine-readable registry.


The single repository-owned Implementation/fix handoff contract is [`templates/implementation-handoff.md`](templates/implementation-handoff.md). Coordinator, Development Workflow, and Implementation all use that same source; do not create a role-local or transport-local competing template.

For new repository work, all roles use the same Git-native lifecycle:

> implementation branch + commit -> GitHub pull request -> review of the exact PR head SHA -> integration of that reviewed head

GitHub branch/commit/PR identity is the authoritative code artifact and GitHub PR is the review surface. Asana remains an orchestration/status surface. Do not create new patch-only handoffs.

Existing patch-based work already in flight may finish under the legacy flow or be converted to a PR. Once converted, the PR head SHA is the active review/integration identity and the patch identity is provenance only.

## GitHub agent attribution

When an agent writes to GitHub through credentials or an account that belongs to Marco, the GitHub artifact must make the agent authorship explicit so human and agent actions are not confused.

This applies to agent-authored:

- pull request descriptions or edits;
- pull request reviews;
- pull request and issue comments or replies;
- inline review comments;
- other GitHub discussion text written through Marco's credentials.

Use a short footer in the authored text:

> `— Dish Agent: <role> | <host>`

Examples:

> `— Dish Agent: Review | ChatGPT`
>
> `— Dish Agent: Implementation | Codex`

When task identity materially helps disambiguate concurrent work, append it:

> `— Dish Agent: Review | ChatGPT | task 1234567890`

Marco's own human-authored GitHub discussion does not require this footer. The footer identifies the acting agent role/host only; it does not change GitHub authentication identity, grant approval authority, or replace exact PR-head identity requirements.

Do not add this footer to ordinary commit-message prose. Commit authorship/signing policy is separate.

## Execution-host boundary

Role and execution host are separate concerns. The same Dish role may run under ChatGPT, Claude Code, or Codex, but host-specific transport/bootstrap policy does not transfer with the role.

- **ChatGPT agents** use the connected GitHub integration as source/history authority and may perform branch, commit, PR, and Review operations through connector-native GitHub actions when the standing role authorizes them. Integration V1-A final landing is the explicit exception: only the local Claude/Codex Integration host may perform reconciliation/merge, as defined by `integration.md`.
- **Claude Code and Codex** do **not** inherit ChatGPT-only connector/bundle instructions. They use their live checkout and host-native `git`/worktree tooling/environment unless Marco gives an explicit task-specific override.
- Local worktrees are an execution-isolation mechanism, not a different artifact contract. The branch, commit SHA, PR URL, and exact PR head SHA are the shared identities across hosts.
- Do not copy ChatGPT connector setup or dependency-bundle bootstrap into a Claude Code/Codex handoff merely because the same standing Dish role is being delegated.

## Branch and direct-commit baseline

- New agent-owned implementation branches normally use `agent/<short-task-slug>` unless the handoff establishes another repository convention.
- One implementation agent owns semantic branch changes at a time; stale/merged/abandoned branches are not reused for unrelated work.
- Terminal cleanup is owned by the repository PR lifecycle controller after authoritative merged/closed/abandoned/superseded disposition. It must fail closed on dirty, unpublished-only, moved/reused, protected, or ambiguous lineage; manual cleanup is only for residual anomalies the controller cannot safely resolve.
- Direct-to-`main` commits are not the default. Marco may explicitly authorize a specific emergency override; roles must state which normal gate is being waived.

Rules:

- when a handoff says to assume or act as a named role, read this index and then the mapped contract before acting;
- role contracts contain stable policy; task handoffs should contain only the task-specific delta;
- do not infer a standing contract from a nearby filename or silently combine incompatible role policies;
- if a requested recurring role is not listed, use root/architecture guidance plus the explicit task handoff and flag the missing standing contract when it materially affects execution;
- a local-checkout agent (Claude Code, Codex) must record its own current role locally for provenance — see [`identity.md`](identity.md); this does not apply to ChatGPT, and it is never authoritative.

## Decision and actor provenance

For bounded attribution/approval interpretation, use [`operator-provenance.md`](operator-provenance.md); this keeps service actor metadata distinct from human authorization without duplicating the full rule into every Project kernel.


Keep these durable provenance classes distinct:

- **Human decision** — an explicit Marco/authorized-human decision with an independent durable source when consequential.
- **Standing repository policy** — current Git authority in the owning contract/ADR/runbook.
- **Agent inference/recommendation** — analysis or recommendation; never settled human/product/cutover policy merely because the write used Marco's account.
- **Runtime observation** — measured current state, not policy by itself.
- **Authenticated-account metadata** — Asana/GitHub `created_by`, comment/PR author, commit author/committer, or similar service actor fields. These prove account attribution, not that Marco physically performed or approved the action when agents/tools can use his credentials.

Consequential human-origin claims require independent provenance such as a current chat instruction, explicit durable human marker, session/host provenance, or suitable platform audit evidence. Never treat `created_by == Marco` alone as human authorization, ownership transfer, or a Review verdict. Agent-authored durable discussion writes retain `Dish Agent: <role> | <host>` provenance where applicable.

When policy and runtime facts conflict, reconcile and surface the discrepancy; do not invent a new human decision.
