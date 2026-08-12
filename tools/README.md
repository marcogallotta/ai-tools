# tools/

## `tools/git-commit`

A stdlib-only Python script (no venv needed to run it directly). It shells out to `git` via
`subprocess` and does not import `dish_tool` or the Asana SDK.

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
worktree root and worktree registry entry, intended non-`main` branch, candidate `HEAD`, and the
captured `refs/heads/main`. Repository-resolution environment overrides such as `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_COMMON_DIR`, alternate index/ref namespace variables, and discovery
overrides are refused rather than silently ignored. The worktree/index must start clean.

Each mailbox message is split and parsed with Git's `mailsplit`/`mailinfo`, applied to the index
without creating a commit, then committed through the worktree's own `tools/git-commit` with
explicit changed paths. Author name/email/date and the mailbox commit message are preserved.
This is why the supported path does not invoke raw `git am`: `git am` creates commits itself and
would bypass the project commit mechanism. This is a targeted integration rule, not a blanket ban
on `git am` for unrelated Git use.

The boundary re-verifies candidate identity and the captured main ref immediately before mutation
and after each apply/commit. If a patch cannot apply cleanly, the already-committed prefix remains
on the integration branch, the failed patch is not committed, and the tool stops. If `main` moves
unexpectedly during the operation, the tool fails as soon as that movement is observed and does
not continue the series. It intentionally does not reset or rewrite `main`, because an unexpected
movement may belong to another actor and must be investigated rather than overwritten.

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
  `-C`, `GIT_DIR`, `GIT_WORK_TREE`, wrong branch/worktree identity, explicit main refusal/main
  immutability, and detection of an unexpected main movement during the commit window.

The suite runs in `tools/.venv` (it has `pytest` installed alongside the SDK, per
`requirements.txt`), so no separate test venv is needed:

```sh
.venv/bin/python -m pytest tests
```
