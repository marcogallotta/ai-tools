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

The script also imports `dish_tool` (advisory checks, DB helpers) as source directly from
`../dish/`, so a `dish/` checkout must be present alongside `tools/` — no `dish/.venv` is
needed for `tools/asana` itself (that venv is only for running `dish` directly; see
`dish/README.md`).

This isn't incidental reuse: `AdvisoryGuard` writes bypass events to the *same* audit DB dish
itself uses, and uses dish's own `COOKING_PROJECT_GID`/`EXCLUDED_SECTION_GIDS` constants so
"managed" means the same thing in both places. Duplicating these instead of importing would
let the two definitions drift and silently break correlation between dish's audit log and
`tools/asana`'s bypass records — don't decouple this without replacing that shared DB/definition,
not just the import.

## Tests

`tools/tests/` covers both scripts:

- `test_batch_plan.py`, `test_error_and_parsing.py`, `test_commands.py` cover `tools/asana`
  (batch-plan validation, error mapping, command handlers, CLI dispatch). They don't need the
  real Asana SDK or network/DB access — `tools/tests/conftest.py` installs a stub `asana`
  module into `sys.modules` before loading the script, and replaces the advisory guard with a
  no-op recorder.
- `test_git_commit.py` covers `tools/git-commit` as a black box: it runs the real script as a
  subprocess against throwaway git repos in `tmp_path` and asserts on exit codes, stderr, and
  the resulting commits/index — staging, `--amend`, carpet-bomb refusal, the staged-deletion
  path, and the `DISH_VERSION` guard.

Both run in `tools/.venv` (it has `pytest` installed alongside the SDK, per
`requirements.txt`), so no separate test venv is needed:

```sh
.venv/bin/python -m pytest tests
```
