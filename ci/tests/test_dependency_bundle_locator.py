from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dependency_bundle_locator import LocatorError, validate  # noqa: E402

SHA = "a" * 40
BUNDLE = "ai-tools-python-deps-v1-test"


def _status(state: str = "success"):
    return {"sha": SHA, "statuses": [{"context": "Dish / dependency bundle", "state": state, "target_url": "https://github.com/x/y/actions/runs/77"}]}


def _run(event: str = "workflow_dispatch"):
    return {"id": 77, "event": event, "path": ".github/workflows/dependency-bundle-mirror.yml", "head_branch": "main", "status": "completed", "conclusion": "success", "repository": {"full_name": "x/y"}}


def _artifacts(expired: bool = False):
    return {"artifacts": [{"id": 88, "name": BUNDLE, "expired": expired}]}


def test_locator_binds_unique_status_run_and_live_artifact() -> None:
    assert validate(status=_status(), run=_run(), artifacts=_artifacts(), repository="x/y", default_branch="main", sha=SHA, bundle_id=BUNDLE)["artifact_id"] == 88


def test_locator_rejects_stale_or_duplicate_discovery() -> None:
    try:
        validate(status=_status(), run=_run(), artifacts=_artifacts(expired=True), repository="x/y", default_branch="main", sha=SHA, bundle_id=BUNDLE)
    except LocatorError as exc:
        assert "exactly one live artifact" in str(exc)
    else:
        raise AssertionError("expired artifact accepted")
