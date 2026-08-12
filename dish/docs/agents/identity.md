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
    "branch": "agent/example-task"
  }
}
```

`active_worktree` is optional compatibility/recovery metadata written by `tools/agent-worktree`; older records without it remain valid. The task-keyed worktree record is the local lifecycle record. Neither record is authoritative task assignment, and `active_worktree` must not be interpreted as a heartbeat or proof that its recorded agent is still running.

## Staleness: not yet solved

There is deliberately no `last_alive`/check-in field yet. A field that only updates when an agent
remembers to rewrite it is not a real freshness signal, and filesystem mtime is no better — both
are only as fresh as the last explicit write, and nothing currently writes one automatically mid
-session. Treat every record here as "true as of `assigned_at`," nothing more; do not infer whether
the registering instance is still running from this file alone.

The planned real fix is a `PostToolUse` hook (supported by both Claude Code and Codex, configured
per-host — `.claude` hooks vs `.codex/hooks.json`) that touches the file automatically on tool use,
so staleness can eventually be judged from real activity instead of an unenforced promise. Not yet
built. Until it is, do not document or rely on a check-in/refresh behavior that doesn't exist.

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
