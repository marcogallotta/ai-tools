# Dish agent role index

This is the canonical router for standing Dish agent roles. Root `CLAUDE.md` points named-role work here so role discovery does not require hard-coded routing in multiple files.

All repository-modifying roles inherit [`contributor-base.md`](contributor-base.md). Specialist contracts add their own scope and authority rules.

| Role / common names | Standing contract |
|---|---|
| Coordinator, master, orchestration coordinator | [`coordinator.md`](coordinator.md) |
| Implementation agent, fix agent | [`implementation.md`](implementation.md) |
| Integration agent, local integrator, patch applier | [`integration.md`](integration.md) |
| Patch reviewer, review specialist | [`review.md`](review.md) |
| Workflow specialist, workflow agent | [`workflow.md`](workflow.md) |
| PostgreSQL specialist, dark-launch specialist, dark-launch agent, PostgreSQL agent | [`postgresql-dark-launch.md`](postgresql-dark-launch.md) |

## Execution-host boundary

Role and execution host are separate concerns. The same Dish role may run under ChatGPT, Claude Code, or Codex, but host-specific transport/bootstrap policy does not transfer with the role.

- ChatGPT agents may use the connected GitHub integration and the GitHub Actions dependency-bundle retrieval path defined in root `CLAUDE.md`.
- Claude Code and Codex do **not** inherit those ChatGPT-only connector/bundle instructions. They use their live checkout and host-native Git/tooling/environment unless Marco gives an explicit task-specific override.
- The Integration agent is currently a local-checkout role because it owns local worktrees, local/environment-specific certification, final `main` promotion, and push verification. Do not reinterpret connector write capability as equivalent integration authority unless the standing contract is deliberately changed.
- Do not copy ChatGPT connector setup or dependency-bundle bootstrap into a Claude Code/Codex handoff merely because the same standing Dish role is being delegated.

Rules:

- when a handoff says to assume or act as a named role, read this index and then the mapped contract before acting;
- role contracts contain stable policy; task handoffs should contain only the task-specific delta;
- do not infer a standing contract from a nearby filename or silently combine incompatible role policies;
- if a requested recurring role is not listed, use root/architecture guidance plus the explicit task handoff and flag the missing standing contract when it materially affects execution;
- a local-checkout agent (Claude Code, Codex) must record its own current role locally for provenance — see [`identity.md`](identity.md); this does not apply to ChatGPT, and it is never authoritative.
