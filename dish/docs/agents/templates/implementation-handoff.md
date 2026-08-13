# Repository implementation handoff

Use this contract for every repository-changing Implementation/fix assignment, including local
publication completion. Coordinator, Development Workflow, and Implementation all reference this
file; Asana may link or mirror it, but it is not the sole policy source.

## Assignment identity

```text
Repository: marcogallotta/ai-tools
Asana task GID: <exact gid>
Authorized branch: agent/<short-task-slug>
Base ref: refs/heads/<target>
Base SHA: <exact 40-char sha>
Existing PR: <#number or none>
Expected PR head: <exact 40-char sha when Existing PR is set; otherwise none>
Assignment class: <implementation | fix | local implementation completion>
```

The repository + Asana task GID + authorized branch + existing-PR/expected-head tuple is one
assignment identity. A matching task on another branch or PR is **not** authorization to adopt,
write, publish, or complete that other lineage. If any element disagrees with live GitHub or durable
local worktree state, stop and reconcile the contradiction rather than choosing a lineage from
memory or convenience.

The supplied base is the authoring base. A later target-branch movement is observed at normal
resume/handoff boundaries; it does not silently replace the base or authorize rebase/reset.

## Local dispatch ownership

Claude Code/Codex repository writers must acquire the matching exclusive local claim before touching
branch/worktree state or executing the implementation agent:

```sh
tools/agent-worktree claim \
  --task <gid> \
  --branch agent/<short-task-slug> \
  --agent-id <local-agent-id> \
  [--pr-number <n> --pr-head <sha> --pr-lease-state active|none [--pr-lease-id <uuid>]] \
  -- <local-agent-process>
```

The claim is a dispatch/start gate, not an advisory note. A live claim prevents a second local agent
from independently writing the same task/branch/PR. PR `dish-agent-lease` markers remain visibility
evidence only and are reconciled against durable task-owned worktree state; they are never sole
ownership authority.

Replacement/fix/publication-completion agents must reconcile and claim the same assignment identity
before touching preserved local state. A different agent may reclaim stale/released ownership only
after explicit orchestration handoff and `claim --takeover`; a still-live owner cannot be bypassed by
takeover. Ambiguous task/worktree/branch/PR ownership fails closed.

## Task-specific delta

Populate only the task-specific goal, scope, constraints, and evidence requirements here or in the
calling handoff. Stable role/process policy stays in the standing contracts.

```text
Goal: <exact outcome>
In scope: <bounded files/subsystem/behavior>
Out of scope: <material exclusions>
Required evidence: <focused tests/checks/environment evidence>
Known dependencies/overlap: <parallel PRs or none>
```

For `local implementation completion`, the existing draft PR must already contain the full
publication-blocker handoff required by `implementation.md`. Continue the same authorized branch and
PR only; do not create a replacement lineage.
