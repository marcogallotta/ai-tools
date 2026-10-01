# Switchstand-managed Dish work

This is the repository bridge for `SWITCHSTAND_MANAGED_DISH`. Switchstand owns the development
process for an explicitly admitted assignment. This document supplies Dish repository context; it
does not copy or replace the Switchstand process manual.

## Work and source linkage

During the proving phase, one current Switchstand `WorkId` identifies the process assignment and
resolves to one exact Asana-backed Dish source task. The source task remains the authority for its
accepted intent and status; the `WorkId` is the stable process identity. When Switchstand exposes a
separate source-task GID or permalink, retain it with the linkage rather than substituting a title.

Read the current `WorkId` through Switchstand authority before acting and preserve that same
`WorkId`/source-task association across re-entry. Never infer it from a task title, Project
membership, tool availability, repository path, host, branch, or PR. Missing, stale, ambiguous, or
conflicting linkage is `UNKNOWN` under the root selector: resolve current work only and perform no
Dish process or mutation until the association is exact.

Git remains source and history authority. Before authoring, verify this repository and the exact
current `main` base required by the assignment. Branch, commit, PR, and head identities attach code
evidence to the linkage; neither the `WorkId` nor this bridge grants write, review, merge, deployment,
or migration authority.

## Repository truth

Start at the [architecture index](architecture/index.md) and load only the routed domain and
authority documents relevant to the change. Use [the runtime contract](runtime-contract.md) when
runtime behavior or safety is affected.

For evidence, use [testing policy](testing.md), the changed-path planner, and
[testing boundaries](architecture/testing-boundaries.md). Load operational sources only when the
assignment reaches that surface—for example [production release](production-release-runbook.md),
[frontend deployment](frontend-deployment-runbook.md), or the relevant database cutover, rollback,
and recovery runbook routed from the architecture index.

Legacy Dish roles, lifecycle/orchestration rules, and ChatGPT Project kernels are not process
authority in this mode. Dish technical, product, data, testing, deployment, rollback, recovery, and
safety invariants remain binding unless current governing evidence and authorized direction change
them. This bridge creates no Project-settings change, workflow or tracker migration, deployment,
audit, cleanup, or activation authority.
