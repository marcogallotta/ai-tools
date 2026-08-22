# tools/

## `tools/git-commit`

A stdlib-only Python script (no venv needed to run it directly). It shells out to `git` via
`subprocess` and does not import `dish_tool` or the Asana SDK.

`--staged-only` is the guarded already-staged mode used by mailbox integration. It does not run
`git add`; instead it requires the complete staged path set to match the explicit named paths
exactly before committing. Normal callers continue to use the ordinary explicit-path staging mode.

## `tools/git-mailbox-integrate.py`

`git-mailbox-integrate.py` is the fail-closed mutation boundary for legacy mailbox work that must
be applied in a dedicated integration worktree. It is deliberately narrow: new work still uses
the branch/commit/PR workflow, and this tool does not redesign branch lifecycle or PR integration.

The historical mailbox incident's exact root cause is not confirmed. Investigation for this guard
reproduced the demonstrated risk class on Git 2.47.3: `git -C` aimed at the wrong worktree and
`GIT_DIR`/`GIT_WORK_TREE` repository-resolution overrides can make an otherwise valid mailbox
application update `refs/heads/main`. The same investigation observed failed `git am` state under
the linked worktree's own gitdir rather than main's gitdir, so the guard does **not** depend on a
theory that `git am` in-progress state is shared repository-wide.

The supported legacy mailbox procedure is:

```sh
python3 tools/git-mailbox-integrate.py \
  --worktree /absolute/path/to/integration-worktree \
  --branch integration-branch \
  -C /absolute/path/to/integration-worktree \
  /absolute/path/to/series.mbox
```

`-C` is optional and defaults to `--worktree`; when supplied it must resolve to that exact
worktree. Mailbox paths are resolved from the caller's real `$PWD`, so keep the mailbox outside
the clean integration worktree.

Before any mutation, the tool binds and verifies the resolved repository/common gitdir, exact
worktree root and worktree registry entry, intended non-`main` branch, candidate `HEAD`, captured
`refs/heads/main`, and the resolved Git executable. Repository-resolution/configuration environment
overrides such as `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, alternate index/ref namespace
variables, `GIT_EXEC_PATH`, external diff injection, and `GIT_CONFIG_*` injection are refused rather
than silently inherited. The worktree/index must start clean.

The approved `tools/git-commit` bytes are verified against candidate `HEAD` and copied into a
private temporary directory **before any mailbox patch is applied**. A mailbox patch may therefore
modify `tools/git-commit` as candidate content, but that uncommitted/candidate version is never
executed while applying the series. The trusted copy invokes the new `--staged-only` mode with
options before `--` and explicit paths after it, so leading-dash filenames remain data and the
commit mechanism does not re-stage candidate content.

Immediately after the Git executable is bound, the executor establishes one sanitized Git
environment and uses it for **every** repository Git subprocess, including boundary verification,
the initial clean-worktree status, mailbox parsing/apply, staged-path inspection, and the trusted
commit subprocess. System/global config is excluded; hooks are redirected to a private empty
directory; `core.fsmonitor` and commit signing are disabled at command-config precedence; and diff
inspection explicitly disables external diff and textconv helpers. Local repository config remains
available for ordinary repository identity/index semantics, but it cannot re-enable those executable
surfaces inside this workflow.

Each mailbox message is split and parsed with Git's `mailsplit`/`mailinfo`, applied to the index
without creating a commit, then committed through that trusted pre-mutation copy of
`tools/git-commit`. Author name/email/date and the mailbox commit message are preserved. The trusted
commit child is pinned to the already-resolved Git executable and inherits the same sanitized Git
environment. This is why the supported path does not invoke raw `git am`: `git am` creates commits
itself and would bypass the project commit mechanism. This is a targeted integration rule, not a
blanket ban on `git am` for unrelated Git use.

The boundary re-verifies candidate identity and the captured main ref immediately before mutation
and after each apply/commit. If a patch cannot apply cleanly, the already-committed prefix remains
on the integration branch, the failed patch is not committed, and the tool stops. If `main` moves
unexpectedly because of an unrelated concurrent actor, the tool fails as soon as that movement is
observed and does not continue the series. It intentionally does not reset or rewrite `main`,
because an unexpected movement may belong to another actor and must be investigated rather than
overwritten.

## `tools/asana` setup

`tools/asana` runs under its own virtualenv (`tools/.venv`), which pins the `python-asana`
SDK. It re-execs into that venv automatically, and fails closed if it's missing. Create it
from a fresh checkout:

```sh
cd tools
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The script imports Dish's read-only `CookingMutationGuard` and Cooking project constants
as source directly from `../dish/`, so a `dish/` checkout must be present alongside `tools/`.
It does not open the Dish database and it is not a second workflow client.

Generic Asana reads remain available. Every generic write performs live read-only membership
lookups first and fails closed when the target is a governed Cooking task, a managed Cooking
section, or cannot be classified safely. Writes in the explicitly excluded Reference and
Sourcing sections remain available. Governed mutations must go through `dish` or `dish-admin`.

## Tests

`tools/tests/` covers the tool scripts:

- `test_batch_plan.py`, `test_error_and_parsing.py`, `test_commands.py` cover `tools/asana`
  (batch-plan validation, error mapping, command handlers, CLI dispatch). They don't need the
  real Asana SDK or network/DB access — `tools/tests/conftest.py` installs a stub `asana`
  module into `sys.modules` before loading the script, and replaces the Cooking mutation guard
  with a no-op recorder. Separate contract tests exercise the real generated SDK methods over a
  controlled low-level transport.
- `test_git_commit.py` covers `tools/git-commit` as a black box: it runs the real script as a
  subprocess against throwaway git repos in `tmp_path` and asserts on exit codes, stderr, and
  the resulting commits/index/remotes — staging, main-branch auto-push and failure verification,
  carpet-bomb refusal, the staged-deletion path, and the `DISH_VERSION` guard.
- `test_git_mailbox_integrate.py` covers the linked-worktree mailbox boundary against throwaway
  repositories: clean application, committed-prefix behavior on a later patch failure, wrong
  `-C`, `GIT_DIR`, `GIT_WORK_TREE`/`GIT_CONFIG_*` overrides, wrong branch/worktree identity,
  explicit main refusal/main immutability, candidate replacement of `tools/git-commit`, active
  post-commit hooks, malicious local `core.fsmonitor` configuration before the first status call,
  leading-dash paths, and type-change-only patches.
- `test_agent_worktree_*.py` (with `agent_worktree_support.py`) cover the shared local-agent lifecycle with throwaway bare origin +
  primary + linked-worktree fixtures: exact-base creation/fetch without moving local refs,
  collisions/concurrent start/interrupted creation recovery, clean/dirty resume, identity mismatch,
  remote ahead/divergence, explicit owned-branch publication/handoff, takeover, conservative cleanup,
  hostile Git environment/configuration, and exact worktree entry.

The suite runs in `tools/.venv` (it has `pytest` installed alongside the SDK, per
`requirements.txt`), so no separate test venv is needed:

```sh
.venv/bin/python -m pytest tests
```

## `tools/agent-worktree`

`agent-worktree` is the stdlib-only, host-independent lifecycle boundary for local Claude Code/Codex
implementation work in this repository. It combines task-owned linked-worktree/branch management with
origin freshness; there is intentionally no separate `git-sync`/`sync-main` workflow.

The tool must be invoked from the real `marcogallotta/ai-tools` primary or linked checkout. It binds
the resolved Git executable, verifies exact top-level/git-dir/common-dir/worktree-registry identity,
normalizes the `origin` repository identity, and refuses repository/config redirection such as
`GIT_DIR`, `GIT_WORK_TREE`, alternate index/ref namespaces, `GIT_CONFIG_*`, or `url.*.insteadOf`.
Normal SSH/credential configuration remains available for private-GitHub authentication. Hooks and
fsmonitor are disabled for the tool's own Git subprocesses.

Task state is written atomically under:

```text
~/.local/state/dish/worktrees/<task_gid>.json
```

The linked worktree defaults to
`~/.local/share/dish/worktrees/ai-tools/<task_gid>/`, configurable with `DISH_WORKTREE_ROOT`, and is
created locked. If `--agent-id` is supplied, the already-existing
`~/.local/state/dish/agents/<agent_id>.json` record is preserved and gains an `active_worktree`
reference to the task record.

### Start and enter

The orchestrator supplies the exact intended base. First creation requires that exact pair to still
match authoritative origin; a moved target is a stale handoff, not permission to choose a newer
base automatically.

```sh
tools/agent-worktree start \
  --task <task_gid> \
  --branch agent/<short-task-slug> \
  --base-ref refs/heads/main \
  --base <exact-40-char-sha> \
  --agent-id <local-agent-id>

tools/agent-worktree exec --task <task_gid> -- <agent-command>
```

`start` serializes first creation with a task-scoped local lock. It refuses branch/path/task-state
collisions, an owned branch checked out elsewhere, an existing remote owned branch without matching
state, detached ownership, and stale/missing/ambiguous origin evidence. If the exact base object is
missing locally, it uses a no-destination `fetch --no-write-fetch-head` shape and proves the object
exists without moving local `main` or another task branch.

### Adopt an explicitly handed-off remote PR branch

When a ChatGPT-created `agent/*` PR branch already exists on verified origin but has no local task
state, an orchestrator can hand its exact identity to a local agent without bypassing this lifecycle:

```sh
tools/agent-worktree adopt \
  --task <task_gid> \
  --branch agent/<existing-pr-branch> \
  --base-ref refs/heads/main \
  --base <original-authoring-base-sha> \
  --expected-head <exact-current-remote-pr-head> \
  --agent-id <local-agent-id>
```

`adopt` is a fail-closed first-state operation, not arbitrary branch takeover. It requires no existing
task state or local branch, verifies the canonical repository and remote branch, requires the supplied
authoring base to be an ancestor of the handed-off head, and re-reads the exact remote branch before
and after creating a locked linked worktree. Any head mismatch before creation leaves no task
state/worktree mutation; a post-create verification race is rolled back locally. Adoption never
resets, rebases, merges, or pushes. On success the durable task record starts with local, published,
and remote-owned heads all equal to the verified handed-off head, after which normal
`resume`/`commit`/`publish`/`verify-handoff`/`cleanup` behavior is unchanged.

### Supersede an old task lineage explicitly

When current orchestration authority deliberately replaces an older implementation branch with a
different authorized branch/PR under the same task GID, use the explicit supersession transition:

```sh
tools/agent-worktree supersede \
  --task <task_gid> \
  --old-branch agent/<superseded-branch> \
  --old-head <exact-superseded-head> \
  --branch agent/<replacement-pr-branch> \
  --base-ref refs/heads/main \
  --base <replacement-authoring-base-sha> \
  --expected-head <exact-replacement-pr-head> \
  --pr-number <replacement-pr-number> \
  --pr-head <exact-replacement-pr-head> \
  --pr-lease-state none \
  --agent-id <local-agent-id> \
  --reason <supersession-reason> \
  --provenance <authority-reference>
```

`supersede` is the only cross-lineage task-state transition. Ordinary `start`, `adopt`, `resume`,
`claim`, takeover, and cleanup keep rejecting branch mismatches. It requires a clean exact old
worktree, exact old and replacement remote heads, no conflicting live local claim or visible active
PR lease, and an exact recoverable old remote branch. Before retiring any old local state it durably
archives the old branch/head/base and ownership provenance with `disposition=superseded`; the old
remote branch is preserved. Replacement activation reuses the normal adoption validation. A crash
after old terminalization but before replacement activation leaves `lifecycle=supersession-incomplete`;
`status` reports that state and only an exact retry of the same `supersede` identity may complete it.

### Resume and status

```sh
tools/agent-worktree resume --task <task_gid> --agent-id <local-agent-id>
tools/agent-worktree resume --task <task_gid> --agent-id <replacement-id> --takeover
tools/agent-worktree status --task <task_gid>
```

Resume re-verifies the task record against the actual worktree/common-dir/git-dir/registry branch and
HEAD, then observes origin at that explicit boundary. Dirty files/index are preserved. The tool never
automatically resets, checks out, merges, or rebases the owned branch. A target `main` movement after
creation is reported only. Local-ahead owned work is normal; remote-ahead or divergent owned branches
fail closed for explicit recovery. `--takeover` is only a provenance update after an explicit
orchestration handoff; it does not infer that the previous agent died.

`status` is local/read-only and does not contact origin.

### Commit explicit task paths

```sh
tools/agent-worktree commit --task <task_gid> -m <message> -- <explicit paths...>
```

`commit` requires the live task claim and re-verifies the stored worktree, common-dir, Git-dir,
`agent/*` branch, and exact pre-commit head. It rejects repository-root/carpet staging, path escape,
and any already-staged path outside the named file/directory scopes. It stages only those literal
paths, applies the same Dish protocol/schema version guard used by `tools/git-commit`, constructs one
commit whose single parent is the verified old head, and attaches it with an exact-old-head
`update-ref` compare-and-swap. A competing branch move therefore leaves the candidate commit
unattached and fails closed. The command never pushes; publication remains a separate lifecycle
step.

### Publish and handoff

```sh
tools/agent-worktree publish --task <task_gid>
tools/agent-worktree verify-handoff --task <task_gid>
```

Publish pushes only
`refs/heads/<owned-branch>:refs/heads/<owned-branch>` to the verified origin URL, without force or
implicit push-target inference, then requires the remote owned head to equal local `HEAD`.
`verify-handoff` refuses dirty or unpublished/mismatched state and reports the stored authoring base,
local implementation head, remote owned head, and current target head separately. A moved target is
not an automatic rebase instruction.

### Cleanup

After GitHub/Asana authority has already established disposition:

```sh
tools/agent-worktree cleanup --task <task_gid> --disposition merged
tools/agent-worktree cleanup --task <task_gid> --disposition closed
tools/agent-worktree cleanup --task <task_gid> --disposition abandoned
tools/agent-worktree cleanup --task <task_gid> --disposition superseded
```

Cleanup refuses dirty/ignored task-local state, unpublished-only recovery state, ambiguous worktree/branch
identity, and any explicit terminal head that no longer matches the remote `agent/*` branch. The terminal
controller supplies the exact PR number, branch, and head; cleanup journals each destructive step before
continuing, removes the linked worktree non-force, conditionally deletes the exact local branch, and deletes
the exact remote `agent/*` branch with `--force-with-lease` expected-head protection. It verifies each
applicable deletion and retains the task record with `dish-terminal-cleanup-v1` historical provenance so a
restart can reconcile a crash between steps. Merged work may use the current target branch as recovery;
closed/abandoned/superseded work must remain recoverable from the terminal owned branch until deletion.
The tool never treats the supplied disposition flag as proof of PR/task state; the caller must establish
terminal authority from current GitHub/Asana state first. Default, protected, non-`agent/*`, moved, or reused
remote refs are never eligible for automatic terminal deletion.

All non-`exec` commands accept `--json` for machine-readable output.

### Pre-Review installed-host continuation launch identity

Hook/config/install-wiring candidates can require real installed Claude/Codex evidence before Review. That continuation stays on the same task/branch/PR and uses the same `agent-worktree` writer fence, but a missing/current local identity may not be invented by a hook. The configured local Implementation launcher writes one fresh mode-0600 JSON record under:

```text
~/.local/state/dish/launch-provenance/<launch-id>.json
```

with schema `dish-local-implementation-launch-v1`. The record binds the actual host session identity (`claude_session_id` or `codex_thread_id`) to the already-authorized `implementation` role, task, project, repository checkout, branch, PR number, and exact PR head. It is launch provenance, not assignment authority.

The exact continuation claim then consumes it explicitly:

```sh
tools/agent-worktree claim \
  --task <task_gid> \
  --branch agent/<existing-pr-branch> \
  --agent-id <actual-host-session-id> \
  --launch-provenance ~/.local/state/dish/launch-provenance/<launch-id>.json \
  --require-launch-provenance \
  --pr-number <pr-number> \
  --pr-head <exact-current-pr-head> \
  --pr-lease-state <active|none> \
  [--pr-lease-id <lease-id>] \
  -- <local-implementation-command>
```

The claim validates the canonical repository, exact remote branch head, PR identity, role/task/project/branch tuple, host-specific identity-source label, launch timestamp, provenance path/permissions, and any visible `CODEX_THREAD_ID` before writing the per-agent identity note. Missing, stale, malformed, noisy, shell-like, or conflicting provenance fails before the child command. `--require-launch-provenance` never falls back to a pre-existing unrelated identity. The per-agent identity remains non-authoritative; live orchestration/GitHub plus the claim decide what may be mutated.

Once inside that claimed local continuation, the canonical real-host entrypoint is one command:

```sh
tools/dish-hook-certify --pr <pr-number> --head <exact-pr-head>
```

It derives the Claude/Codex active hook surface, runs the selected evidence, drives required real-host children, and publishes/readbacks the exact-head certificate. One-time host auth/container setup and failure handling are in [`../ci/hook-certification.md`](../ci/hook-certification.md).
