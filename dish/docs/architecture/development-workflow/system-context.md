# Development Workflow system context

## Read this when

Read this when changing actors, external systems, execution hosts, or trust/capability boundaries in the development workflow.

## Scope

This document describes who participates and which system boundary each participant crosses. It does not grant role authority or prescribe operating commands.

## Current architecture

Marco supplies product intent and consequential design/risk decisions. Specialist and Implementation roles turn accepted work into GitHub candidates. Independent Review evaluates an exact candidate. CI and local certification supply evidence. A separately authorized local Integration role lands a reviewed candidate. Asana carries live orchestration and design state; GitHub carries source and PR lifecycle facts; runtime truth requires direct environment evidence.

The same semantic role may run on ChatGPT, Claude Code, or Codex, but hosts expose different repository transport and environment capabilities. Host choice never composes another role.

## Invariants

- GitHub, Asana, repository policy, and runtime observations remain distinct authority surfaces.
- Review is independent of material authorship for the reviewed candidate.
- Final V1-A Integration landing occurs only on an authorized local Claude/Codex host.
- A connected tool's ability to write does not grant role or mutation authority.
- Marco is not the standing scheduler, poller, transcript courier, or line-by-line reviewer.
- Manual relay is represented truthfully when no supported automated path exists.

## Current anchors

- [Standing roles](../../agents/index.md)
- [Operator control plane](../../../../OPERATOR_CONTROL_PLANE.md)
- [Implementation handoff](../../agents/templates/implementation-handoff.md)
- [Historical, never-activated dispatcher design](../../../../ci/pr-lifecycle-dispatcher-runbook.md)

## Related documents

- [Authority and state](authority-and-state.md)
- [Execution hosts and operator boundary](execution-hosts-and-operator-boundary.md)
- [Lifecycle](lifecycle.md)
