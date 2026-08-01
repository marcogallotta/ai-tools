import hashlib
import json
import tarfile
from pathlib import Path

from migration import import_migrated_durable_state as durable
from migration import prepare_corpus_target_import as target


ROOT = Path(__file__).resolve().parents[1]
BATCH_ARCHIVE = ROOT / "migration/batch-002-correction-4-codex-verified.tgz"
LEGACY_ARCHIVE = ROOT / "migration/corpus-migration-pre-batch-002-v3.tgz"
SCHEMA = ROOT / "tests/fixtures/dish-version-current/dish-task-schema.json"


def _status_file(tmp_path: Path) -> tuple[Path, str]:
    with tarfile.open(BATCH_ARCHIVE, "r:gz") as tf:
        manifest = json.load(tf.extractfile(
            "dish_migration_batch_002_correction_4/manifest-batch-002.json"
        ))
    rows = [
        {
            "source_gid": row["source_gid"],
            "task_name": row["source_name"],
            "proposed_status": "pending-research",
            "concise_reason": "Test-only migration assignment.",
            "confidence": "high",
            "marco_question": None,
        }
        for row in manifest
    ]
    path = tmp_path / "statuses.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _registry(manifest: list[dict]) -> dict[str, str]:
    names = sorted({
        row.get("proposed_target_section_name") or row["captured_section_name"]
        for row in manifest
    } | {"Sourcing"})
    return {name: str(900_000 + index) for index, name in enumerate(names)}


def test_blueprint_has_all_103_tasks_and_approved_canh_destination(tmp_path):
    statuses, digest = _status_file(tmp_path)
    output = tmp_path / "out"
    result = target.main([
        "--batch-archive", str(BATCH_ARCHIVE),
        "--legacy-archive", str(LEGACY_ARCHIVE),
        "--schema", str(SCHEMA),
        "--approved-statuses", str(statuses),
        "--approved-statuses-sha256", digest,
        "--source-overrides", str(ROOT / "migration/production-source-overrides.json"),
        "--output-dir", str(output),
        "--blueprint-only",
    ])
    assert result == 0
    blueprint = json.loads((output / "production-project-blueprint.json").read_text())
    assert blueprint["governed_tasks"] == 99
    assert blueprint["unmanaged_sourcing_tasks"] == 4
    assert blueprint["total_tasks"] == 103
    assert {item["name"] for item in blueprint["sections"]} == {
        "Desserts", "Eating", "Fish", "Hunan", "Indonesia/Malaysia", "Isan/Lao",
        "Japanese", "Korean", "Levant", "Maghreb", "Mediterranean", "Persian",
        "Planned", "Seasonal", "Sichuan", "Sourcing", "Subcontinent", "Thai",
        "Verification Queue", "Vietnamese",
    }
    canh = next(
        item for item in blueprint["tasks"] if item["source_gid"] == "1216522297757193"
    )
    assert canh["destination_section_name"] == "Eating"


def test_render_and_durable_preparation_use_proposed_destination(tmp_path):
    statuses_path, digest = _status_file(tmp_path)
    statuses = target.load_approved_statuses(statuses_path, digest)
    with tarfile.open(BATCH_ARCHIVE, "r:gz") as tf:
        governed = target.load_governed(tf, statuses)
        with tarfile.open(BATCH_ARCHIVE, "r:gz") as manifest_tf:
            manifest = json.load(manifest_tf.extractfile(
                "dish_migration_batch_002_correction_4/manifest-batch-002.json"
            ))
        registry = _registry(manifest)
        schema = json.loads(SCHEMA.read_text())
        rendered = target.render_governed(tf, governed, registry, schema)
    canh = next(item for item in rendered if item.source_gid == "1216522297757193")
    assert f"Destination section: Eating — {registry['Eating']}" in canh.notes

    batch_dir = tmp_path / "batch"
    with tarfile.open(BATCH_ARCHIVE, "r:gz") as tf:
        tf.extractall(batch_dir)
    batch_dir = batch_dir / "dish_migration_batch_002_correction_4"
    assignments = {}
    for item in governed:
        assignment = target.state_assignment(item, item.source_gid)
        assignments[item.source_gid] = assignment
    assignments_path = tmp_path / "durable-assignments.json"
    assignments_path.write_text(json.dumps(assignments), encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    prepared = durable._prepare_tasks(
        batch_dir=batch_dir,
        schema_path=SCHEMA,
        assignments_path=assignments_path,
        registry_path=registry_path,
        expected_count=99,
    )
    durable_canh = next(item for item in prepared if item.source_gid == "1216522297757193")
    assert f"Destination section: Eating — {registry['Eating']}" in durable_canh.notes
