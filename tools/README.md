# tools/

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

## Tests

`tools/tests/` covers `tools/asana`: batch-plan validation, error mapping, command handlers,
and CLI dispatch. `tools/git-commit` has no test suite yet.

The tests don't need the real Asana SDK or network/DB access — `tools/tests/conftest.py`
installs a stub `asana` module into `sys.modules` before loading the script, and replaces the
advisory guard with a no-op recorder. They still run in `tools/.venv` (it has `pytest`
installed alongside the SDK, per `requirements.txt`), so no separate test venv is needed:

```sh
.venv/bin/python -m pytest tests
```
