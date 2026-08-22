# ADR 0005: Execution choices are capability-grounded

Status: Accepted

## Read this when

Read this when choosing a host, declaring work local-only, introducing a manual relay, or depending on an external platform feature.

## Context

ChatGPT, Claude Code, Codex, Worker, GitHub, and Asana expose different transport, environment, permission, context, convergence, and cost boundaries. Assuming unavailable automation creates non-executable designs; ignoring a proved material local advantage can also waste scarce context, infrastructure, and publication/convergence capacity. Routing local by convenience still consumes scarce operator/local capacity.

## Decision

Mechanisms are designed against verified current capabilities and material execution costs of their target host/surface. Semantic Implementation remains remote/hosted by default. Coordinator may recommend local execution when current evidence shows a concrete material advantage, including required infrastructure/local-only capability, a proved large-file/payload/model-context boundary, required shared local state, or materially lower convergence/publication cost. The evidence and expected benefit must be named; overlap, duration, prior local use, preference, or convenience alone is insufficient.

The recommendation is non-causal and non-authorizing: it creates no dependency or WAIT, changes no SEND NOW classification, grants no local dispatch or semantic role authority, and waives no exact lineage, claim, Review, certification, or Integration gate. Native tests and local system access remain separate classifications unless semantic source mutation is actually part of the work. Manual relay is explicit when it is the real supported path.

## Consequences

Host selection follows evidence, not runtime duration, previous agent location, or convenience. When no concrete material local advantage exists, the normal hosted route remains in force. Missing capabilities and positive local benefits are named accurately; neither grants alternative role authority or creates fictional automation.

## Related documents

- [Execution hosts and operator boundary](../execution-hosts-and-operator-boundary.md)
- [System context](../system-context.md)
