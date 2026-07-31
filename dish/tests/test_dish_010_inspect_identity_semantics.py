from __future__ import annotations
from tests.support.verification import make_app



def test_inspect_names_baseline_and_current_identity_comparison_separately(tmp_path):
    app, _backend, operation_id, _protocol = make_app(tmp_path)

    inspected = app.execute(
        "inspect",
        agent="gpt",
        submission_id=operation_id,
    )

    assert inspected["ok"], inspected
    content = inspected["data"]["content"]
    view = inspected["data"]["authoritative_view"]

    assert "expected_identity" not in content
    assert content["operation_baseline_identity"] == inspected["data"]["operation"][
        "expected_identity"
    ]
    assert content["operation_baseline_identity"] != content["confirmed_identity"]

    assert content["confirmed_identity"] == content["required_identity"]
    assert content["live_identity"] == content["required_identity"]
    assert content["identity_matches"] is True
    assert content["required_identity"] == view["required_identity"]
    assert content["live_identity"] == view["live_identity"]
    assert content["identity_matches"] == view["identity_matches"]
