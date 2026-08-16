from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_lifecycle_host_routing import (  # noqa: E402
    CHATGPT_IMPLEMENTATION,
    IMPLEMENTATION_PUBLICATION,
    LOCAL_IMPLEMENTATION,
    LOCAL_SYSTEM_ACCESS,
    TESTS_ONLY,
    classify_requirement,
    implementation_host_for_boundary,
)


def test_long_test_runtime_never_changes_tests_only_work_type():
    boundary = classify_requirement("TESTS ONLY — run the 45-minute native suite")
    assert boundary.work_type == TESTS_ONLY
    assert implementation_host_for_boundary(boundary) == CHATGPT_IMPLEMENTATION


def test_local_system_access_never_becomes_local_semantic_implementation():
    boundary = classify_requirement("LOCAL SYSTEM ACCESS — verify installed systemd unit")
    assert boundary.work_type == LOCAL_SYSTEM_ACCESS
    assert implementation_host_for_boundary(boundary) == CHATGPT_IMPLEMENTATION


def test_local_implementation_requires_exact_unavailable_capability_and_fallbacks():
    boundary = classify_requirement(
        "IMPLEMENTATION / PUBLICATION — hosted branch transport cannot publish governed file; "
        "fallbacks exhausted: GitHub connector update, Git data API"
    )
    assert boundary.work_type == IMPLEMENTATION_PUBLICATION
    assert boundary.unavailable_remote_capability == "hosted branch transport cannot publish governed file"
    assert boundary.fallbacks_exhausted == ("GitHub connector update", "Git data API")
    assert implementation_host_for_boundary(boundary) == LOCAL_IMPLEMENTATION


def test_unstructured_local_implementation_label_is_not_local_only_proof():
    boundary = classify_requirement("run local generator", default_kind="implementation")
    assert boundary.work_type == IMPLEMENTATION_PUBLICATION
    assert boundary.local_implementation_eligible is False
    assert implementation_host_for_boundary(boundary) == CHATGPT_IMPLEMENTATION
