from pathlib import Path


def test_protocol_hook_contract(tmp_path):
    # Hook content is tested from an Honest tree supplied by the test environment.
    # The local docs own mechanics; this test keeps the required wording explicit.
    activation = (Path(__file__).resolve().parents[1] / "docs" / "dish-tool-activation.md").read_text()
    assert "Phase boundaries" in activation
    assert "tool pass" in activation.lower()
    assert "tooling failure" in activation.lower()
