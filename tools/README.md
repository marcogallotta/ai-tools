# tools/

## `tools/git-commit`

A stdlib-only Python script (no venv needed to run it directly). It shells out to `git` via
`subprocess` and does not import `dish_tool` or the Asana SDK.

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

`tools/tests/` covers both scripts:

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

Both run in `tools/.venv` (it has `pytest` installed alongside the SDK, per
`requirements.txt`), so no separate test venv is needed:

```sh
.venv/bin/python -m pytest tests
```
