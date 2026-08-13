from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

DISH_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = DISH_ROOT / "scripts" / "chatgpt_project_kernels.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_project_kernels", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
kernels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kernels)

EXPECTED_ROLES = {
    "coordinator", "development-workflow", "implementation", "review", "integration",
    "workflow", "postgresql-dark-launch",
}
EXPECTED_EVALS = {
    "stale-project-version", "live-authority-over-stale-memory", "review-exact-head-completion",
    "coordinator-pr-intake-automatic-review", "reviewed-head-movement-classification",
    "implementation-rejects-patch-only-completion", "integration-rejects-head-mismatch",
    "current-template-lookup", "handoff-conflicts-with-role-authority",
    "allowed-specialist-implementation-composition", "forbidden-implicit-role-expansion",
    "task-history-before-no-op", "valid-action-fallback", "no-valid-fallback",
    "cross-role-context-bleed",
}


def _observations(scenario_id: str) -> list[dict[str, object]]:
    if scenario_id == "review-exact-head-completion":
        return [
            {"seq": 1, "kind": "durable_write", "operation": "pull_request_review", "method": "COMMENT", "pr": 41, "head_sha": "H", "write_id": "review-41"},
            {"seq": 2, "kind": "readback", "operation": "pull_request_review", "pr": 41, "head_sha": "H", "write_id": "review-41", "verified": True},
        ]
    if scenario_id == "valid-action-fallback":
        return [
            {"seq": 1, "kind": "capability_discovery", "operation": "pull_request_review", "available_methods": ["COMMENT"], "unavailable_methods": ["APPROVE"]},
            {"seq": 2, "kind": "durable_write", "operation": "pull_request_review", "method": "COMMENT", "pr": 42, "head_sha": "abc123", "write_id": "review-42"},
            {"seq": 3, "kind": "readback", "operation": "pull_request_review", "pr": 42, "head_sha": "abc123", "write_id": "review-42", "verified": True},
        ]
    return []


def _passing_behavior_results() -> dict[str, object]:
    manifest, _ = kernels.load_canonical()
    results = []
    for scenario in kernels._evals():
        for role in scenario["roles"]:
            case_id = f"{scenario['id']}::{role}"
            results.append({
                "case_id": case_id,
                "fresh_chat_id": f"fresh::{case_id}",
                "assistant_response": {
                    "outcome": scenario["expected_outcome"],
                    "actions": list(scenario["required_actions"]),
                },
                "runner_observations": _observations(scenario["id"]),
            })
    return {
        "schema_version": 2,
        "runner_protocol": "dish-chatgpt-project-behavior-v2",
        "canonical_version": manifest["canonical_version"],
        "results": results,
    }


def _result(payload: dict[str, object], case_id: str) -> dict[str, object]:
    results = payload["results"]
    assert isinstance(results, list)
    return next(item for item in results if item["case_id"] == case_id)


def test_manifest_source_rendered_identity_and_current_topology_are_bound() -> None:
    manifest, source = kernels.load_canonical()
    kernels.validate_topology(source)
    assert set(source["roles"]) == EXPECTED_ROLES
    assert manifest["canonical_version"].startswith("dish-chatgpt-projects-v2-")
    assert len(manifest["source_sha256"]) == 64
    assert len(manifest["kernel_identity_sha256"]) == 64
    assert manifest["kernel_identity_sha256"] == kernels.kernel_identity(source)
    assert manifest["canonical_version"].endswith(manifest["kernel_identity_sha256"][:12])


def test_rendered_identity_changes_when_behavioral_template_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _, source = kernels.load_canonical()
    current = kernels.kernel_identity(source)
    monkeypatch.setattr(kernels, "STARTUP_TEMPLATE", kernels.STARTUP_TEMPLATE + " Changed behavior.")
    assert kernels.kernel_identity(source) != current


def test_generated_kernels_are_current_and_within_budget() -> None:
    manifest, source = kernels.load_canonical()
    results = kernels.render_all(check=True)
    assert {role for role, _ in results} == EXPECTED_ROLES
    assert all(count <= manifest["max_kernel_chars"] for _, count in results)
    for role, path in kernels.generated_paths(manifest, source).items():
        text = path.read_text(encoding="utf-8")
        assert f"PROJECT_CANONICAL_VERSION: {manifest['canonical_version']}" in text
        assert "PROJECT INSTRUCTIONS STALE" in text
        assert source["roles"][role]["contract"] in text


def test_eval_contracts_cover_approved_scenarios_and_action_evidence_oracles() -> None:
    assert set(kernels.validate_eval_contracts()) == EXPECTED_EVALS
    by_id = {scenario["id"]: scenario for scenario in kernels._evals()}
    fallback = by_id["valid-action-fallback"]
    assert fallback["require_ordered_observations"] is True
    assert fallback["observation_link_field"] == "write_id"
    assert [item["kind"] for item in fallback["required_observations"]] == [
        "capability_discovery", "durable_write", "readback"
    ]
    assert fallback["required_observations"][1]["equals"] == {
        "method": "COMMENT", "pr": 42, "head_sha": "abc123"
    }
    assert fallback["required_observations"][2]["equals"]["verified"] is True


def test_prepared_cases_are_fresh_and_hide_decision_and_observation_oracles() -> None:
    bundle = kernels.prepare_eval_bundle()
    expected_case_count = sum(len(scenario["roles"]) for scenario in kernels._evals())
    assert bundle["runner_protocol"] == "dish-chatgpt-project-behavior-v2"
    assert "newly created chat" in bundle["fresh_chat_requirement"]
    assert "runner-observed tool evidence" in bundle["response_contract"]["instruction"]
    assert len(bundle["cases"]) == expected_case_count == 19
    for case in bundle["cases"]:
        assert kernels.ORACLE_FIELDS.isdisjoint(case)
        assert len(case["kernel_sha256"]) == 64
        assert "PROJECT_CANONICAL_VERSION" in case["project_instructions"]


def test_behavioral_evaluator_accepts_complete_fresh_chat_results() -> None:
    assert len(kernels.evaluate_behavior_results(_passing_behavior_results())) == 19


def test_action_labels_alone_cannot_satisfy_self_owned_comment_review() -> None:
    payload = _passing_behavior_results()
    result = _result(payload, "valid-action-fallback::review")
    result["runner_observations"] = []
    with pytest.raises(kernels.KernelError, match="missing runner-observed evidence"):
        kernels.evaluate_behavior_results(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda obs: obs[1].update(method="APPROVE"), "missing required runner observation"),
        (lambda obs: obs[1].update(head_sha="wrong-head"), "missing required runner observation"),
        (lambda obs: obs[2].update(write_id="different-write"), "do not share write_id"),
        (lambda obs: (obs[1].update(seq=3), obs[2].update(seq=2)), "missing required runner observation"),
    ],
)
def test_self_owned_review_observations_fail_wrong_transport_head_link_or_order(mutation, message: str) -> None:
    payload = _passing_behavior_results()
    result = _result(payload, "valid-action-fallback::review")
    observations = result["runner_observations"]
    assert isinstance(observations, list)
    mutation(observations)
    with pytest.raises(kernels.KernelError, match=message):
        kernels.evaluate_behavior_results(payload)


def test_behavioral_evaluator_rejects_reused_chat_and_forbidden_action() -> None:
    payload = _passing_behavior_results()
    results = payload["results"]
    assert isinstance(results, list)
    results[1]["fresh_chat_id"] = results[0]["fresh_chat_id"]
    with pytest.raises(kernels.KernelError, match="reused fresh_chat_id"):
        kernels.evaluate_behavior_results(payload)

    payload = _passing_behavior_results()
    first = payload["results"][0]
    scenario = next(s for s in kernels._evals() if f"{s['id']}::{s['roles'][0]}" == first["case_id"])
    first["assistant_response"]["actions"].append(scenario["forbidden_actions"][0])
    with pytest.raises(kernels.KernelError, match="behavior eval failed"):
        kernels.evaluate_behavior_results(payload)


def test_fresh_chat_runner_preserves_runner_observations_for_judging(tmp_path: Path) -> None:
    fixtures = {}
    for result in _passing_behavior_results()["results"]:
        fixtures[result["case_id"]] = {
            "fresh_chat_id": result["fresh_chat_id"],
            "assistant_response": result["assistant_response"],
            "runner_observations": result["runner_observations"],
        }
    runner = tmp_path / "fake_fresh_chat_runner.py"
    runner.write_text(
        "import json, sys\n"
        f"FIXTURES = json.loads({json.dumps(fixtures, sort_keys=True)!r})\n"
        "case = json.load(sys.stdin)\n"
        "assert 'required_observations' not in case\n"
        "print(json.dumps(FIXTURES[case['case_id']]))\n",
        encoding="utf-8",
    )
    payload = kernels.run_fresh_chat_runner(f"python {runner}")
    assert len(kernels.evaluate_behavior_results(payload)) == 19
    fallback = _result(payload, "valid-action-fallback::review")
    assert [event["kind"] for event in fallback["runner_observations"]] == [
        "capability_discovery", "durable_write", "readback"
    ]


def test_version_comparison_fails_stale_and_accepts_current() -> None:
    manifest, _ = kernels.load_canonical()
    current, current_message = kernels.version_status(manifest["canonical_version"])
    stale, stale_message = kernels.version_status("dish-chatgpt-projects-v1-stale")
    assert current is True and "PROJECT INSTRUCTIONS CURRENT" in current_message
    assert stale is False and "PROJECT INSTRUCTIONS STALE" in stale_message
    assert manifest["canonical_version"] in stale_message


def test_role_composition_is_explicit_and_does_not_expand_authority() -> None:
    manifest, source = kernels.load_canonical()
    implementation = kernels.render_role(manifest, source, "implementation")
    workflow = kernels.render_role(manifest, source, "workflow")
    review = kernels.render_role(manifest, source, "review")
    assert "exactly one specialist contract" in implementation
    assert "explicitly assigned repository implementation" in workflow
    assert "No implicit role composition is permitted." in review
    assert "Review does not implement fixes" in review
