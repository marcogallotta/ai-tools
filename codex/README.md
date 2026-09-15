# Codex local-agent hooks

`hooks.json` is installed at user level because Codex has no project-local hook
configuration. Dish operator context and investigation limits remain scoped to
`~/ai-tools`. The small primary-checkout Git mutation deny applies to visible
direct Git commands in any repository, including Switchstand; linked writer
worktrees remain writable. The operator `SessionStart`
entry invokes `~/.local/bin/dish-operator-context`, so exact-head certification
can bind both the user hook definition and the operator-policy adapter to the
same candidate worktree. `SessionStart(source=compact)` invokes
`~/.local/bin/agent-reground`, which reloads current Dish role/process authority
plus owning Asana and active Git/PR state from durable identity. Re-grounding is
informational recovery and is not a global tool-use barrier. The Bash-specific
hook calls
`~/.local/bin/codex-protected-checkout` and preserves its hard-deny boundary.
The same adapter handles `PermissionRequest` only as a compatibility fallback.
There is no blanket Git prompt rule. `codex/default.rules` replaces the old
personal rules containing routine prompts and the blanket `git add` refusal.
Its explicit severe forms are forbidden, not presented as habitual approvals.
The primary-checkout deny is applied in `PreToolUse`, before execution.
Claude's `destructive-op-guard` uses the same small branch check.

The Bash `PreToolUse` entry also invokes `~/.local/bin/investigation-guard`. That guard keeps a
session/task-local monotonic investigation count outside model reasoning and can enter
`CHECKPOINT_REQUIRED` only when a trace-derived calibration for the admitted task class exists.
Without such calibration it remains observe-only. The current Codex repository configuration
qualifies **Bash only** for this invariant; MCP, built-in non-Bash tools, opaque child-process work,
or any tool class not proven to emit and honor `PreToolUse` remains `DEGRADED` and must not be
reported as hard-closed. A separately calibrated deep-research class must be selected at admission
(e.g. by the session launcher/environment); the model cannot renew or self-promote the ordinary
investigation envelope.

The installed executable resolves the checked-in
`hooks/investigation-guard-calibration.json` beside itself. That policy is mechanically derived
from the repository trace fixtures, so a fresh symlink installation activates the qualifying
Claude/Codex narrow-fix caps as the ordinary default without a separate per-user calibration step. An explicit
`DISH_INVESTIGATION_CALIBRATION` path remains available for controlled calibration tests.

The shared `hooks/protected_checkout.py` classifier denies direct and visibly
nested `git checkout`/`git switch` branch changes against the primary
`~/ai-tools` worktree. It resolves real Git worktree identity, command-line and
environment repository redirects, and resolvable Git aliases. It also denies
persistent/interactive shell launch from the protected primary checkout so a
later `write_stdin` cannot become an unobserved command channel.

This is a command-hook guardrail, not a process or filesystem sandbox. Under
`danger-full-access`, an opaque script, Make target, Python subprocess, or other
child process can invoke Git without exposing that Git command to `PreToolUse`.
Execpolicy also cannot inspect the semantic authorization of an Asana write,
deployment, merge, or equivalent wrapped command. Current task authority and
the owned-worktree managed launcher remain the stronger boundaries. Do not claim
that removing prompts makes unrestricted execution safe.

## Install after integration

Link the merged files from the primary checkout, then start a new Codex
session and use `/hooks` to review and trust the exact hook definition:

```sh
ln -s /home/marco/ai-tools/codex/hooks.json /home/marco/.codex/hooks.json
ln -s /home/marco/ai-tools/codex/git-pr.rules /home/marco/.codex/rules/git-pr.rules
ln -s /home/marco/ai-tools/codex/default.rules /home/marco/.codex/rules/default.rules
ln -s /home/marco/ai-tools/hooks/dish-operator-context /home/marco/.local/bin/dish-operator-context
ln -s /home/marco/ai-tools/hooks/agent-reground /home/marco/.local/bin/agent-reground
ln -s /home/marco/ai-tools/hooks/codex-protected-checkout /home/marco/.local/bin/codex-protected-checkout
ln -s /home/marco/ai-tools/hooks/investigation-guard /home/marco/.local/bin/investigation-guard
```

Do not overwrite existing paths blindly. The existing personal `default.rules`
is a regular, untracked file: preserve it before replacing it with the reviewed
symlink. Inspect all existing user hooks/rules; multiple sources run and an old
`prompt` rule still overrides a new `allow` rule. Test the installed rules with
`codex execpolicy check` and start a fresh Codex session before claiming prompts
have disappeared. Review any app-specific approval settings separately.

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

Temporarily point the user hook, rules and adapter links at that exact worktree head.
First inspect `~/.codex/hooks.json`, `~/.codex/rules/git-pr.rules`, `~/.codex/rules/default.rules`, `~/.local/bin/dish-operator-context`,
`~/.local/bin/agent-reground`, `~/.local/bin/codex-protected-checkout`, and `~/.local/bin/investigation-guard`;
move aside and later restore any pre-existing files or links rather than
overwriting them.

```sh
ln -s "$WT/codex/hooks.json" /home/marco/.codex/hooks.json
ln -s "$WT/codex/git-pr.rules" /home/marco/.codex/rules/git-pr.rules
ln -s "$WT/codex/default.rules" /home/marco/.codex/rules/default.rules
ln -s "$WT/hooks/dish-operator-context" /home/marco/.local/bin/dish-operator-context
ln -s "$WT/hooks/agent-reground" /home/marco/.local/bin/agent-reground
ln -s "$WT/hooks/codex-protected-checkout" /home/marco/.local/bin/codex-protected-checkout
ln -s "$WT/hooks/investigation-guard" /home/marco/.local/bin/investigation-guard

test "$(readlink -f /home/marco/.codex/hooks.json)" = "$WT/codex/hooks.json"
test "$(readlink -f /home/marco/.local/bin/dish-operator-context)" = "$WT/hooks/dish-operator-context"
test "$(readlink -f /home/marco/.local/bin/investigation-guard)" = "$WT/hooks/investigation-guard"
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
