from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_local_role_index_projects_friction_and_debt_capture_contracts():
    index = (ROOT / "dish/docs/agents/index.md").read_text(encoding="utf-8")
    contributor = (ROOT / "dish/docs/agents/contributor-base.md").read_text(encoding="utf-8")
    asana_mode = (ROOT / "dish/docs/agents/asana-v2-project-mode.md").read_text(encoding="utf-8")

    for text in (index, contributor):
        assert "1217443500915644" in text
        assert "1217443501022227" in text
        assert "notice -> dedupe -> log/update -> continue" in text

    assert "bounded non-V2 capture writes" in contributor
    assert "Bounded legacy capture writes" in asana_mode
    assert "move or dispatch work" in asana_mode
