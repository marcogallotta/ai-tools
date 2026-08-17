from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/native_postgresql_waivers.py"
SPEC = importlib.util.spec_from_file_location("native_postgresql_waivers", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_full_route_reuses_exact_head_canonical_waiver_serializer():
    values = module.serialized_waivers()
    assert values == module.pr_certification.NATIVE_WAIVERS
    assert tuple(json.loads(value) for value in values) == module.pr_certification.NATIVE_WAIVER_RECORDS
    assert module.waiver_cli_args()[::2] == ("--waive-skip",) * len(values)


def test_focused_route_reuses_exact_head_selection_semantics():
    selected_file = "tests/postgresql/native/test_process_failure_command.py"
    values = module.serialized_waivers(test_files=(selected_file,))
    assert values == module.pr_certification._native_waivers_for_selection(
        mode="focused", test_files=[selected_file]
    )
    assert len(values) == 1
    assert json.loads(values[0])["nodeid"].startswith(selected_file + "::")
