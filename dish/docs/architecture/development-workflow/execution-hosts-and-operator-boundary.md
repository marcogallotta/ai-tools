# Execution hosts and operator boundary

## Read this when

Read this when choosing or changing an execution host, publication route, local-only classification, handoff UX, or operator interaction.

## Scope

This document records architectural host and human-attention boundaries. It does not duplicate host commands or presentation templates.

## Current architecture

Semantic role and execution host are separate. ChatGPT uses connected GitHub/Asana capabilities and repository-owned remote publication paths. Claude Code and Codex use a live checkout plus native Git and the repository-owned worktree lifecycle. Worker is an execution mode whose authority comes from the routed standing role, not from its name. Final V1-A Integration landing remains local-only.

Remote/hosted Implementation is the normal semantic authoring path. Local authoring requires proof of an unavailable remote source/publication capability and bounded exhausted fallbacks. Native tests remain `TESTS ONLY`; sudo, systemd, devices, and installed runtime access remain `LOCAL SYSTEM ACCESS`. Elapsed time and convenience do not change those classes.

Marco owns outcomes, priorities, consequential design/risk/cost, and explicit exceptional authority. Agents own the authorized inner loop: grounding, diagnosis, retries, supported recovery, reconciliation, and routine technical choices. They interrupt Marco only when his answer materially changes the allowed or desirable path and current authority, evidence, and ordinary engineering judgment cannot resolve it.

Operator communication is an intent-first projection, not a mandatory status packet. Routine execution/status carries the smallest useful result or action. Design and Review lead with direction, then expose the alternatives, tradeoffs, risks, and evidence needed for the active judgment; RCA and requested deep dives retain the reasoning they need. Truth, safety, authority boundaries, and material caveats always remain, but empty owner/risk/action categories are not manufactured. Equivalent task or PR states collapse unless one changes Marco's decision or action, while durable PR/Asana/files/logs retain the complete evidence.

Progressive-disclosure corrections apply immediately: expansion adds relevant depth, while requests for less detail or less jargon re-render or continue without an acknowledgement-only turn. A progress update is never a completion point while authorized actionable work remains; `continue` or `resume`, including after a premature-stop correction, resumes execution before any optional explanation. A handoff is ordinary task input rather than authority or an adversarial signal: the receiver silently grounds current role, authority, and artifact, owns routine stale-mechanic recovery, and escalates only a concrete unresolved identity, authority, capability, or safety boundary.

## Invariants

- Host capability never expands semantic role authority.
- Routine investigation, recovery, and engineering choice do not become operator interruptions merely because they are uncertain or require retries.
- Consequential product/design/risk/cost/authority choices, destructive/production/external effects, unrecoverable input, material safety changes, and unavoidable manual relays remain operator boundaries.
- Concision or progressive disclosure changes presentation only; it never hides material truth or ends unfinished authorized work.
- Missing automation is not converted into a permanent Marco relay duty.
- Handoffs are executable only after mandatory durable identities are resolved and read back.
- A local completion route preserves the existing task/branch/PR lineage and the exact bounded residual work.
- Directly shown instructions use a copy-ready block for chat transport or a stable full temporary-file path for local Claude/Codex transport where the owning contract requires it.
- Manual handoffs are locator-first and contain only non-reconstructable payload. Inline transfer is
  permitted only at or below both deterministic limits: eight non-empty lines and 700 characters.
  Crossing either limit uses a complete private temporary file for local Claude/Codex or a supported
  transferable artifact for ChatGPT; unavailable ChatGPT artifact transport remains an explicit
  capability blocker rather than a long chat fallback.
- A revised handoff is the complete replacement artifact, never an addendum, and no manual relay
  means no forced copy-block or file ceremony.
- Credentials, production changes, destructive actions, and consequential design choices retain their independent authorization boundaries.

## Current anchors

- [`../../agents/implementation.md`](../../agents/implementation.md)
- [`../../agents/identity.md`](../../agents/identity.md)
- [`../../../../OPERATOR_CONTROL_PLANE.md`](../../../../OPERATOR_CONTROL_PLANE.md)
- [`../../../../tools/agent-worktree-handoff.md`](../../../../tools/agent-worktree-handoff.md)

## Related documents

- [System context](system-context.md)
- [Work identity and concurrency](work-identity-and-concurrency.md)
- [ADR 0005](decisions/0005-capability-grounded-execution.md)
