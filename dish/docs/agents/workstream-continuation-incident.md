# Workstream continuation incident — PR #206

This file is incident evidence for the shared role-index invariant. It is not a second lifecycle or a task-specific replacement for the governing Review V3 design.

## Failure

Review V3 `review-v3-g5` explicitly authorized ordered Implementation slices under one semantic contract. PR #206 implemented one slice. During the fix sequence, a durable Implementation handoff stated that the fix completed the full approved G5 contract, but the local Implementation execution later rewrote the PR scope to Slice 3 only and treated the remaining slices as future ordered work. Review then accepted the narrowed Slice-3 scope and issued a merge verdict for that slice.

The individual Slice-3 result can be valid while the governing workstream is still incomplete. The failure was allowing candidate-local scope prose and a member-level verdict to become an implicit stopping rule for the parent objective.

## Required invariant

A PR is a member/review unit, not authority to redefine or complete its governing assignment. When durable authority defines one ordered multi-PR or multi-slice workstream, every agent must preserve the parent objective until the authoritative completion condition is satisfied. A member may finish, pass Review, or merge without the workstream being complete.

A semantic scope reduction requires new durable scope authority. Implementation, local completion, Review, and Integration cannot create that authority by rewriting PR prose, accepting a smaller diff, or calling remaining members future work.

The canonical standing rule lives in `dish/docs/agents/index.md`; this incident record exists only to keep the demonstrated regression recoverable.
