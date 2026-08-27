# Codex local-agent hooks

`hooks.json` is installed at user level because Codex has no project-local hook
configuration. Every adapter immediately exits unless the session CWD is
`~/ai-tools` or one of its descendants, so Dish policy and enforcement never
apply to unrelated repositories. The operator `SessionStart`
entry invokes `~/.local/bin/dish-operator-context`, so exact-head certification
can bind both the user hook definition and the operator-policy adapter to the
same candidate worktree. `SessionStart(source=compact)` invokes
`~/.local/bin/agent-reground`, which reloads current Dish role/process authority
plus owning Asana and active Git/PR state from durable identity. Re-grounding is
informational recovery and is not a global tool-use barrier. The Bash-specific
hook calls
`~/.local/bin/codex-protected-checkout` and preserves its hard-deny boundary.
The same adapter handles `PermissionRequest`: direct Git reads, ordinary
mutations on a known non-`main` branch, and direct `gh pr` commands are allowed;
`main`, unresolved, wrapped, compound, and unfamiliar forms retain the prompt.
Claude's `destructive-op-guard` uses the same small branch check.

The shared `hooks/protected_checkout.py` classifier denies direct and visibly
nested `git checkout`/`git switch` branch changes against the primary
`~/ai-tools` worktree. It resolves real Git worktree identity, command-line and
environment repository redirects, and resolvable Git aliases. It also denies
persistent/interactive shell launch from the protected primary checkout so a
later `write_stdin` cannot become an unobserved command channel.

This is a command-hook guardrail, not a process or filesystem sandbox. Under
`danger-full-access`, an opaque script, Make target, Python subprocess, or other
child process can invoke Git without exposing that Git command to `PreToolUse`.
The owned-worktree session launcher is responsible for the stronger isolation
boundary.

## Install after integration

Link the merged files from the primary checkout, then start a new Codex
session and use `/hooks` to review and trust the exact hook definition:

```sh
ln -s /home/marco/ai-tools/codex/hooks.json /home/marco/.codex/hooks.json
ln -s /home/marco/ai-tools/codex/git-pr.rules /home/marco/.codex/rules/git-pr.rules
ln -s /home/marco/ai-tools/hooks/dish-operator-context /home/marco/.local/bin/dish-operator-context
ln -s /home/marco/ai-tools/hooks/agent-reground /home/marco/.local/bin/agent-reground
ln -s /home/marco/ai-tools/hooks/codex-protected-checkout /home/marco/.local/bin/codex-protected-checkout
```

Do not overwrite existing paths blindly. Inspect and preserve any existing
user hook configuration before installation; multiple hook sources run rather
than replacing one another.

## Exact-head local runtime certification

Certification must use the reviewed PR head, not whatever happens to be on
`main`. Set `WT` to the owned worktree and `EXPECTED` to the reviewed PR head,
then verify the identity before starting Codex:

```sh
WT=/home/marco/.local/share/dish/worktrees/ai-tools/1217425634694989
EXPECTED=<reviewed-pr-head-sha>
test "$(git -C "$WT" rev-parse HEAD)" = "$EXPECTED"
git -C "$WT" status --short
```

Temporarily point the user hook and adapter links at that exact worktree head.
First inspect `~/.codex/hooks.json`, `~/.codex/rules/git-pr.rules`, `~/.local/bin/dish-operator-context`,
`~/.local/bin/agent-reground`, and `~/.local/bin/codex-protected-checkout`;
move aside and later restore any pre-existing files or links rather than
overwriting them.

```sh
ln -s "$WT/codex/hooks.json" /home/marco/.codex/hooks.json
ln -s "$WT/codex/git-pr.rules" /home/marco/.codex/rules/git-pr.rules
ln -s "$WT/hooks/dish-operator-context" /home/marco/.local/bin/dish-operator-context
ln -s "$WT/hooks/agent-reground" /home/marco/.local/bin/agent-reground
ln -s "$WT/hooks/codex-protected-checkout" /home/marco/.local/bin/codex-protected-checkout

test "$(readlink -f /home/marco/.codex/hooks.json)" = "$WT/codex/hooks.json"
test "$(readlink -f /home/marco/.local/bin/dish-operator-context)" = "$WT/hooks/dish-operator-context"
test "$(git -C "$WT" rev-parse HEAD)" = "$EXPECTED"
```

Start a fresh installed Codex session, open `/hooks`, and confirm the user
operator `SessionStart`, compact `SessionStart`, and protected-checkout
`PreToolUse` matchers are loaded from the exact-head `hooks.json`; review/trust its current hash if
prompted. The first `SessionStart` must execute the candidate-bound
`~/.local/bin/dish-operator-context`, so the injected operator policy comes from
the same `WT`/`EXPECTED` head as the loaded hook definition. Record
`codex --version`, the PR head, and the active sandbox/approval settings
(`danger-full-access` and `on-request` on the machine at implementation time).

From a session rooted in `/home/marco/ai-tools`, confirm each command is denied
before execution and that `git branch --show-current` remains `main`:

```sh
git switch -c agent/cert-denied
git checkout -b agent/cert-denied
git -C /home/marco/ai-tools switch -c agent/cert-denied
git --git-dir=/home/marco/ai-tools/.git --work-tree=/home/marco/ai-tools checkout -b agent/cert-denied
git -c alias.cert='checkout -b agent/cert-denied' cert
bash -lc 'git switch -c agent/cert-denied'
bash
```

The final `bash` check must be denied at launch; no unified-exec session should
exist for a subsequent `write_stdin`. Also capture one real hook input to
confirm Codex supplies `tool_name: "Bash"`, `tool_input.command`, and the
session `cwd`, and confirm the response uses `permissionDecision: "deny"`.

From the exact owned linked worktree, confirm `git switch`/`checkout` remains
available under ordinary approval policy and a persistent shell can launch.
Repeat in a temporary unrelated repository. Do not perform branch mutations in
the primary checkout merely to test the denial. Finally restore the prior user
hook/adapter paths (or, after merge, install the primary-checkout links above).
