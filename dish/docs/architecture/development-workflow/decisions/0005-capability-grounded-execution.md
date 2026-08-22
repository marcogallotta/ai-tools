# ADR 0005: Execution choices are capability-grounded

Status: Accepted

## Read this when

Read this when choosing a host, declaring work local-only, introducing a manual relay, or depending on an external platform feature.

## Context

ChatGPT, Claude Code, Codex, Worker, GitHub, and Asana expose different transport, environment, permission, and cost boundaries. Assuming unavailable automation creates non-executable designs; routing local by convenience consumes scarce operator/local capacity.

## Decision

Mechanisms are designed against verified current capabilities of their target host/surface. Semantic Implementation is remote/hosted by default. Local source work requires an exact unavailable remote capability plus exhausted bounded fallbacks. Native tests and local system access remain separate classes. Manual relay is explicit when it is the real supported path.

## Consequences

Host selection follows evidence, not runtime duration, previous agent location, or convenience. Missing capabilities are named accurately and do not grant alternative role authority or create fictional automation.

## Related documents

- [Execution hosts and operator boundary](../execution-hosts-and-operator-boundary.md)
- [System context](../system-context.md)
