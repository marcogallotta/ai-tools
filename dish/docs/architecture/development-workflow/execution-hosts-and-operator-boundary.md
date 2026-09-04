# Execution hosts and operator boundary

## Read this when

Read this when choosing or changing an execution host, publication route, local-only classification, handoff UX, or operator interaction.

## Scope

This document records architectural host and human-attention boundaries. It does not duplicate host commands or presentation templates.

## Current architecture

Semantic role and execution host are separate. ChatGPT uses connected GitHub/Asana capabilities and repository-owned remote publication paths. Claude Code and Codex use a live checkout plus native Git and the repository-owned worktree lifecycle. Worker is an execution mode whose authority comes from the routed standing role, not from its name. Final V1-A Integration landing remains local-only.

Remote/hosted Implementation is the normal semantic authoring path. Local authoring requires proof of an unavailable remote source/publication capability and bounded exhausted fallbacks. Native tests remain `TESTS ONLY`; sudo, systemd, devices, and installed runtime access remain `LOCAL SYSTEM ACCESS`. Elapsed time and convenience do not change those classes.

Marco owns outcomes, priorities, consequential design/risk, and explicit exceptional authority. Agents own routine mechanics. Durable PR/Asana surfaces carry full agent-to-agent context; Marco-facing communication carries only the outcome, decision/action, material risk, and next owner needed at operator altitude.

A current-chat Marco `override` is authoritative over every repository-owned Dish gate or actor boundary for its exact scope, including rules labeled as safety, security, independence, role, Review, Integration, destructive, or production boundaries. The acting host executes and records the waiver without converting an internal policy conflict into a platform limitation or forcing a new session. This exceptional authority cannot make an unavailable capability available or defeat an external platform/system control that technically prevents the operation.

## Invariants

- Host capability never expands semantic role authority.
- Missing automation is not converted into a permanent Marco relay duty.
- Handoffs are executable only after mandatory durable identities are resolved and read back.
- A local completion route preserves the existing task/branch/PR lineage and the exact bounded residual work.
- Directly shown instructions use a copy-ready block for chat transport or a stable full temporary-file path for local Claude/Codex transport where the owning contract requires it.
- Credentials, production changes, destructive actions, and consequential design choices retain their independent authorization boundaries unless Marco explicitly overrides the exact boundary in the current chat.

## Current anchors

- [`../../agents/implementation.md`](../../agents/implementation.md)
- [`../../agents/identity.md`](../../agents/identity.md)
- [`../../../../OPERATOR_CONTROL_PLANE.md`](../../../../OPERATOR_CONTROL_PLANE.md)
- [`../../../../tools/agent-worktree-handoff.md`](../../../../tools/agent-worktree-handoff.md)

## Related documents

- [System context](system-context.md)
- [Work identity and concurrency](work-identity-and-concurrency.md)
- [ADR 0005](decisions/0005-capability-grounded-execution.md)
