from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from tests.flake_policy import validate_marker_metadata


TODAY = date(2026, 7, 31)


@pytest.mark.smoke
def test_flake_candidate_metadata_is_complete_and_remains_blocking():
    assert validate_marker_metadata(
        "flake_candidate",
        {
            "issue": "DISH-123",
            "owner": "Marco",
            "first_seen": "2026-07-31",
            "signature": "sqlite schema mismatch during teardown",
        },
        today=TODAY,
    ) == []


def test_quarantine_is_time_bounded_and_requires_launch_critical_waiver():
    metadata = {
        "issue": "DISH-123",
        "owner": "Marco",
        "first_seen": "2026-07-29",
        "quarantined_on": "2026-07-31",
        "expires": "2026-08-07",
        "signature": "sqlite schema mismatch during teardown",
    }
    assert validate_marker_metadata(
        "quarantined", metadata, today=TODAY, launch_critical=False
    ) == []
    assert validate_marker_metadata(
        "quarantined", metadata, today=TODAY, launch_critical=True
    ) == ["launch-critical smoke/invariant tests require an explicit waiver"]


def test_expired_or_overlong_quarantine_is_rejected():
    expired = {
        "issue": "DISH-123",
        "owner": "Marco",
        "first_seen": "2026-07-20",
        "quarantined_on": "2026-07-20",
        "expires": "2026-07-27",
        "signature": "race",
    }
    assert "quarantine has expired" in validate_marker_metadata(
        "quarantined", expired, today=TODAY
    )

    overlong = dict(expired)
    overlong.update(
        {
            "quarantined_on": "2026-07-31",
            "expires": "2026-08-08",
        }
    )
    assert "quarantine may last at most 7 days" in validate_marker_metadata(
        "quarantined", overlong, today=TODAY
    )


def test_tests_do_not_enable_automatic_per_test_reruns():
    root = Path(__file__).parent
    violations = []
    for path in sorted(root.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if (
                node.attr == "flaky"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "pytest"
            ):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, (
        "automatic per-test reruns hide nondeterminism; use the diagnostic "
        "rerun lane instead:\n" + "\n".join(violations)
    )


class _Marker:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs


class _Item:
    def __init__(self, nodeid, markers):
        self.nodeid = nodeid
        self._markers = {marker.name: marker for marker in markers}

    def get_closest_marker(self, name):
        return self._markers.get(name)

    def iter_markers(self):
        return iter(self._markers.values())


def test_collection_policy_rejects_dual_candidate_and_quarantine_markers():
    from tests.conftest import _flake_policy_violations

    item = _Item(
        "tests/test_example.py::test_example",
        [
            _Marker(
                "flake_candidate",
                issue="DISH-123",
                owner="Marco",
                first_seen="2026-07-31",
                signature="race",
            ),
            _Marker(
                "quarantined",
                issue="DISH-123",
                owner="Marco",
                first_seen="2026-07-31",
                quarantined_on="2026-07-31",
                expires="2026-08-07",
                signature="race",
            ),
        ],
    )
    assert _flake_policy_violations([item]) == [
        "tests/test_example.py::test_example: cannot be both flake_candidate and quarantined"
    ]
