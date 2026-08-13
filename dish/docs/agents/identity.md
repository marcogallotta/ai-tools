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
  "active_worktree": {
    "task_gid": "1234567890",
    "state_path": "/home/user/.local/state/dish/worktrees/1234567890.json",
    "worktree": "/home/user/.local/share/dish/worktrees/ai-tools/1234567890",
    "branch": "agent/example-task",
    "claim_id": "<exclusive task claim UUID>",
    "pr_number": 123
  }
}
```

`active_worktree` is optional compatibility/recovery metadata written by `tools/agent-worktree`; older records without it remain valid. The task-keyed worktree record is the local lifecycle record and now carries the exclusive local owner claim for its exact assignment. Neither file creates task assignment authority: the explicit implementation handoff and live orchestration/GitHub authority still decide which task/branch/PR lineage may be worked. `active_worktree` is not a heartbeat or proof that its recorded agent is still running.

## Staleness and owner recovery

There is deliberately no `last_alive`/check-in field. Filesystem mtime, silence, and advisory PR lease age are not reliable liveness signals and must not automatically revoke an owner. This avoids a timeout race that could authorize two agents at once.

`tools/agent-worktree` instead gives each newly acquired local task assignment an opaque `claim_id`. A second agent cannot resume, publish, verify, bind the PR, or enter that claimed worktree as the owner. When orchestration has explicitly established a handoff or that the prior owner is abandoned/stale, the replacement performs `resume --takeover --expected-claim <current-claim>`. The task lock serializes the compare-and-set; if the claim changed, recovery fails without changing ownership. Legacy task state without a claim is recoverable only through the explicit `legacy-unclaimed` sentinel. This is ownership fencing, not liveness detection.

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
