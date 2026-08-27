# ADR 0003: Proposed restartable lifecycle dispatcher

Status: Not yet activated

## Read this when

Read this as history when evaluating the never-activated dispatcher design or proposing new PR routing automation.

## Context

The design attempted to make routine PR observation and routing survive agent/session loss without making Marco poll or ferry transcripts. Multiple independent controllers would have raced and disagreed.

## Decision

The repository contains an implementation of the proposed dispatcher in `scripts/pr_lifecycle.py`, but it was never deployed or commissioned and will not be activated. It is not standing workflow infrastructure or authority. Current routing is manual: the acting role re-reads GitHub, the owning Asana task, and the applicable standing contracts at each handoff.

## Consequences

The code and historical runbooks remain reference material only. Agents must not invoke them as the current operating procedure, infer that a background consumer will continue work, or extend them as though a live dispatcher exists. Any future automation would require a new explicit decision and current design review; meanwhile manual handoffs preserve the same exact-head, readback, role-separation, and fail-closed guards.

## Related documents

- [Recovery, observability, and completion](../recovery-observability-and-completion.md)
- [Authority and state](../authority-and-state.md)
