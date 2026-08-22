# Development Workflow extension rules

## Read this when

Read this when a proposal adds or changes development-workflow authority, persistent state, lifecycle machinery, operator ceremony, or a cross-host mechanism.

## Scope

This document explains how to evolve the current system. It does not impose an architecture update for implementation-local refactors that preserve documented boundaries.

## Current architecture

Begin at the canonical [architecture index](../index.md), follow this subtree's routing, and state which documented boundaries the proposal changes. Current architecture lives here only after it is accepted and landed; proposed/future designs remain on their owning Asana task.

When a PR changes a documented boundary, update the owning architecture document or ADR in the same PR by default. A separate ordered documentation PR is exceptional and must not leave current architecture falsely describing the new landed state. Design Review and Code Review challenge contradictions, silent new authorities, and missing architecture impact. Existing audit/health work discovers stale anchors and routes bounded repairs.

## Invariants

- Extend the existing authority or derived consumer before creating another writer, queue, scheduler, database, service, identity system, or control plane.
- Reuse [the lifecycle dispatcher](../../../../scripts/pr_lifecycle.py) and existing gate predicates instead of adding a competing lifecycle controller.
- Put commands in runbooks, role permissions in standing contracts, current architecture here, proposals in Asana, and executable guarantees in code/tests.
- ADRs capture consequential settled choices, not temporary mechanics or generic design principles.
- Capability claims name the actual target host/surface and current evidence; unavailable capability is not assumed into existence.
- Structural tests prove navigation and mechanically checkable shape, not semantic freshness.

## Current anchors

- [Canonical extension rules](../extension-rules.md)
- [Design principles](../../agents/design-principles.md)
- [Development Workflow role](../../agents/development-workflow.md)
- [Architecture knowledge-base tests](../../../tests/test_architecture_knowledge_base.py)

## Related documents

- [Development Workflow index](index.md)
- [Development Workflow decisions](decisions/index.md)
- [Recovery, observability, and completion](recovery-observability-and-completion.md)
