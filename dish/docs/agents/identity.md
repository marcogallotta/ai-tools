# Local agent identity file

A local-checkout agent instance (Claude Code, Codex) must write a file recording its own current
role to:

```
~/.local/state/dish/agents/<agent_id>.json
```

This lives outside the repository entirely — not in the checkout, not gitignored-in-repo, nothing
to commit or accidentally push. It is per-instance: each running agent instance gets its own file,
named by its own `agent_id`, so concurrent instances (e.g. a Claude Code session and a Codex
session working the same checkout at once) never share or clobber a single file the way a
repo-local, fixed-path file would.

This does not apply to ChatGPT, which has no equivalent local filesystem/checkout state and
handles agent identity separately (see root `CLAUDE.md`). Do not extend this mechanism to ChatGPT.

## Authority stays where it already was

- Role contracts in `dish/docs/agents/*.md` (routed from [`index.md`](index.md)) remain the
  authoritative definition of what a role is and does.
- Asana owns task assignment and work state, per the relevant role contract (e.g.
  [`postgresql-dark-launch.md`](postgresql-dark-launch.md)'s live-coordination-authority section).
- This file changes none of that. It is a local note, not a source of truth, and must never be
  read by one agent/session/host as evidence of another's state.

## Shape

```json
{
  "agent_id": "<instance identifier, see below>",
  "role": "postgresql-dark-launch",
  "assigned_at": "2026-08-12T00:00:00Z",
  "notes": "free text",
  "workspace": "optional metadata, e.g. checkout path",
  "owning_task_gid": "1234567890",
  "owning_project_gid": "1217419962189616",
  "active_worktree": {
    "task_gid": "1234567890",
    "state_path": "/home/user/.local/state/dish/worktrees/1234567890.json",
    "worktree": "/home/user/.local/share/dish/worktrees/ai-tools/1234567890",
    "branch": "agent/example-task"
  }
}
```

`owning_task_gid` and `owning_project_gid` are optional durable recovery pointers for the current assignment. They do not create assignment authority. When a mapped standing role names Asana as its live coordination authority, `owning_task_gid` is required for post-compaction re-grounding so the hook can re-read the live owning task rather than reconstructing it from chat. `owning_project_gid` is checked against both the role contract and the task's current project membership when present.

`active_worktree` is optional compatibility/recovery metadata written by `tools/agent-worktree`; older records without it remain valid. Its `task_gid` is also accepted as the owning-task recovery pointer for implementation work. The task-keyed worktree record is the local lifecycle record. The exclusive local claim is stored separately under the task-scoped claim state. Neither record creates task-assignment authority: the explicit implementation handoff and live orchestration/GitHub authority decide which task/branch/PR lineage may be worked. `active_worktree` is not a heartbeat or proof that its recorded agent is still running.

## Staleness and owner recovery

There is deliberately no `last_alive`/check-in field. Filesystem mtime, silence, and advisory PR lease age are not reliable liveness signals and must not automatically revoke an owner.

`tools/agent-worktree claim` gives each acquired local task assignment an opaque claim generation. `tools/agent-worktree status --task <gid> --json` exposes that generation as `claim.claim_id`. After explicit orchestration handoff or stale-owner determination, replacement uses `claim --takeover --expected-claim <claim.claim_id>` and wraps `resume --takeover`. The task/branch/PR locks serialize the compare-and-set; if the durable generation changed, takeover fails without replacing ownership. A still-live owner cannot be bypassed because its locks prevent takeover acquisition. Legacy active task state without a claim record is recoverable only through the explicit `legacy-unclaimed` sentinel.

## Where `agent_id` comes from, per host

Identity is host-specific; there is no shared scheme:

- **Claude Code**: the running session's own instance/conversation identifier.
- **Codex**: the `CODEX_THREAD_ID` environment variable identifies the running thread/instance.
- **ChatGPT**: does not use this mechanism at all — see above.

## Rules

- Never write this file inside the repository checkout.
- Writing this file is not role assignment. An agent still determines its role from the routing in
  `index.md` and the matching contract; this file only records that determination locally after the
  fact.

## Mechanically bound launch provenance for local Implementation continuation

A pre-Review local Implementation continuation that must exercise installed Claude/Codex hooks uses `tools/agent-worktree claim --require-launch-provenance`. The local launcher must first emit a mode-0600 `dish-local-implementation-launch-v1` record under `~/.local/state/dish/launch-provenance/` containing the actual host session identifier and the already-authorized Implementation task/project/branch/PR/head tuple. `agent-worktree` validates that record and only then records the session identity locally.

`hooks/agent-reground` never bootstraps a missing identity. A missing, stale, malformed, ambiguous, or conflicting identity/provenance record remains unresolved and the guard fails closed. Launch provenance is evidence that this host instance is the named session; it still does not create role, task, branch, Review, or Integration authority.
