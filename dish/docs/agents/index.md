# Dish agent role index

This is the canonical router for standing Dish agent roles. Root `CLAUDE.md` points named-role work here so role discovery does not require hard-coded routing in multiple files.

| Role / common names | Standing contract |
|---|---|
| Coordinator, master, orchestration coordinator | [`coordinator.md`](coordinator.md) |
| Implementation agent, fix agent, integrator | [`implementation.md`](implementation.md) |
| Patch reviewer, review specialist | [`review.md`](review.md) |
| Workflow specialist, workflow agent | [`workflow.md`](workflow.md) |

Rules:

- when a handoff says to assume or act as a named role, read this index and then the mapped contract before acting;
- role contracts contain stable policy; task handoffs should contain only the task-specific delta;
- do not infer a standing contract from a nearby filename or silently combine incompatible role policies;
- if a requested recurring role is not listed, use root/architecture guidance plus the explicit task handoff and flag the missing standing contract when it materially affects execution.
