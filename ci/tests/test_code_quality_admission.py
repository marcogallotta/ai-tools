from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from code_quality_admission import exact_head_admission  # noqa: E402
from code_quality_common import SCHEMA, _digest  # noqa: E402

HEAD = "a" * 40
BASE = "b" * 40


def _comment(*, head: str = HEAD, enabled: bool = True, outcome: str = "PASS") -> dict[str, str]:
    result = {
        "schema": SCHEMA,
        "head_sha": head,
        "target_base_sha": BASE,
        "pr_number": 42,
        "effective_enabled": enabled,
        "outcome": outcome,
    }
    result["result_digest"] = _digest(result)
    body = (
        f"<!-- dish-code-quality-result:v1 head={head} digest={result['result_digest']} -->\n"
        f"```json\n{json.dumps(result, sort_keys=True)}\n```"
    )
    return {"body": body}


def test_disabled_on_both_sides_is_nonblocking_without_result() -> None:
    admission = exact_head_admission(
        comments=[], head=HEAD, target_base=BASE, pr_number=42,
        base_policy="version=1\nenabled=false\n", head_policy="version=1\nenabled=false\n",
    )
    assert admission.allowed is True
    assert admission.enabled is False


def test_candidate_activation_requires_exact_head_author_result() -> None:
    missing = exact_head_admission(
        comments=[], head=HEAD, target_base=BASE, pr_number=42,
        base_policy="version=1\nenabled=false\n", head_policy="version=1\nenabled=true\n",
    )
    assert missing.allowed is False
    assert "exactly one" in missing.reason
    assert exact_head_admission(
        comments=[_comment()], head=HEAD, target_base=BASE, pr_number=42,
        base_policy="version=1\nenabled=false\n", head_policy="version=1\nenabled=true\n",
    ).allowed is True


def test_successor_head_invalidates_prior_result() -> None:
    admission = exact_head_admission(
        comments=[_comment()], head="c" * 40, target_base=BASE, pr_number=42,
        base_policy="version=1\nenabled=true\n", head_policy="version=1\nenabled=true\n",
    )
    assert admission.allowed is False


def test_dependency_workflows_keep_candidate_untrusted_and_publish_exact_locator() -> None:
    build = (ROOT / ".github/workflows/dependency-bundle-build.yml").read_text()
    mirror = (ROOT / ".github/workflows/dependency-bundle-mirror.yml").read_text()
    assert "issues:" in build and "dish-dependency-bundle-candidate:v1" in build
    assert "persist-credentials: false" in build
    assert "trusted/scripts/dependency_bundle.py" in build
    assert "candidate/scripts/dependency_bundle.py" not in build
    assert "Dish / dependency bundle" in mirror
    assert "statuses: write" in mirror
    assert "expired == false" in mirror
