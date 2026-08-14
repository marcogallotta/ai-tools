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

`agent-worktree` is the stdlib-only local lifecycle boundary for Claude Code/Codex Implementation
work in this repository. Cross-host writer authority is the repository-owned global Implementation
claim service; `agent-worktree claim` obtains/validates that durable generation first, then layers
task/branch/PR OS locks and task-owned linked-worktree management underneath it. There is intentionally
no separate host ownership authority or `git-sync`/`sync-main` workflow.

Production clients require `DISH_IMPLEMENTATION_CLAIM_URL` and `DISH_IMPLEMENTATION_CLAIM_TOKEN`;
see `ci/implementation-claim-service.md`. The direct SQLite claim adapter is test-only.

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
tools/agent-worktree claim \
  --task <task_gid> \
  --branch agent/<short-task-slug> \
  --base <exact-40-char-sha> \
  --agent-id <local-agent-id> \
  -- \
  tools/agent-worktree start \
    --task <task_gid> \
    --branch agent/<short-task-slug> \
    --base-ref refs/heads/main \
    --base <exact-40-char-sha> \
    --agent-id <local-agent-id>
```

The outer claim must remain active while writer subcommands/agent commands run. `start` serializes first creation with a task-scoped local lock. It refuses branch/path/task-state
collisions, an owned branch checked out elsewhere, an existing remote owned branch without matching
state, detached ownership, and stale/missing/ambiguous origin evidence. If the exact base object is
missing locally, it uses a no-destination `fetch --no-write-fetch-head` shape and proves the object
exists without moving local `main` or another task branch.

### Adopt an explicitly handed-off remote PR branch

When a ChatGPT-created `agent/*` PR branch already exists on verified origin but has no local task
state, an orchestrator can hand its exact identity to a local agent without bypassing this lifecycle:

Adoption is likewise wrapped by `tools/agent-worktree claim`, with the exact base plus PR number/head
passed to the outer claim before the `adopt` subcommand runs. The canonical command shape lives in
`dish/docs/agents/templates/implementation-handoff.md`.

`adopt` is a fail-closed first-state operation, not arbitrary branch takeover. It requires no existing
task state or local branch, verifies the canonical repository and remote branch, requires the supplied
authoring base to be an ancestor of the handed-off head, and re-reads the exact remote branch before
and after creating a locked linked worktree. Any head mismatch before creation leaves no task
state/worktree mutation; a post-create verification race is rolled back locally. Adoption never
resets, rebases, merges, or pushes. On success the durable task record starts with local, published,
and remote-owned heads all equal to the verified handed-off head, after which normal
`resume`/`publish`/`verify-handoff`/`cleanup` behavior is unchanged.

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
fail closed for explicit recovery. `--takeover` is only a provenance update after an explicit orchestration handoff; the outer claim also
requires `--expected-global-claim`, `--takeover-reason`, and `--liveness-evidence`, plus the exact local
`--expected-claim`. The local generation is checked under OS locks before global CAS, so a failed local
takeover cannot move cross-host ownership.

`status` is local/read-only and does not contact origin; its JSON claim block exposes both the local
`claim_id` and durable `global_claim_id`.

### Publish and handoff

```sh
tools/agent-worktree publish --task <task_gid>
tools/agent-worktree verify-handoff --task <task_gid>
```

Before `git push`, publish revalidates the exact durable global generation and journals one
expected-head publication intent. It then pushes only
`refs/heads/<owned-branch>:refs/heads/<owned-branch>` to the verified origin URL, without force or
implicit push-target inference, and completes the journal only after remote readback equals local
`HEAD`. A stale generation is rejected before push; an ambiguous push result is reconciled rather than
blindly retried.
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

Cleanup refuses dirty state, remote-ahead/divergent ambiguity, and any local implementation head that
would become unrecoverable. It unlocks only immediately before a non-force `git worktree remove`,
retains the local branch as a recovery pointer, and retains the task record with its historical
disposition. The tool never treats the supplied disposition flag as proof of PR/task state; the
caller is responsible for establishing that state from current GitHub/Asana authority first.

All non-`exec` commands accept `--json` for machine-readable output.
