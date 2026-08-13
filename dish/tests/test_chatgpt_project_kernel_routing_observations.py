from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

DISH_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = DISH_ROOT / "scripts" / "chatgpt_project_kernels.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_project_kernels_routing", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
kernels = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kernels)


@pytest.mark.parametrize("operation", ["global_pr_search", "web_search_repository"])
def test_runner_observed_forbidden_discovery_is_rejected(operation: str) -> None:
    oracle = {
        "required_observations": [
            {
                "kind": "connector_read",
                "operation": "pull_request_read",
                "equals": {
                    "connector": "GitHub",
                    "repository": "marcogallotta/ai-tools",
                    "pr": 31,
                },
            }
        ],
        "forbidden_actions": {"ask_owner_repo", "global_pr_search", "web_search_repository"},
        "require_ordered_observations": False,
        "observation_link_field": "",
    }
    observations = [
        {"seq": 1, "kind": "external_discovery", "operation": operation},
        {
            "seq": 2,
            "kind": "connector_read",
            "operation": "pull_request_read",
            "connector": "GitHub",
            "repository": "marcogallotta/ai-tools",
            "pr": 31,
        },
    ]

    with pytest.raises(kernels.KernelError, match="runner observed forbidden operation"):
        kernels._validate_observed_evidence("configured-repository-pr-routing::review", oracle, observations)
