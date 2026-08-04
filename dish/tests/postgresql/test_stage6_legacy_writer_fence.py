from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_service.http import build_server
import dish_service.legacy_writer_fence as fence_module
from dish_service.legacy_writer_fence import (
    engage_legacy_writer_fence,
    observe_legacy_writer_fence,
    read_legacy_writer_fence,
    release_legacy_writer_fence,
)
from dish_tool.errors import DishRuleError
from tests.support.service_foundation import _release_loader
from tests.support.thread_teardown import start_server_thread, stop_server
from tests.support.verification import Backend

NOW = datetime(2026, 8, 1, 22, 0, tzinfo=timezone.utc)


def test_legacy_writer_fence_is_atomic_fail_closed_and_digest_bound(tmp_path: Path) -> None:
    path = tmp_path / "state" / "legacy-writer-fence.json"
    path.parent.mkdir()
    manifest, digest = engage_legacy_writer_fence(
        path,
        fence_id="fence-1",
        candidate_id="candidate-1",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    assert read_legacy_writer_fence(path) == (manifest, digest)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert engage_legacy_writer_fence(
        path,
        fence_id="fence-1",
        candidate_id="candidate-1",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )[1] == digest
    committed_bytes = path.read_bytes()
    with pytest.raises(DishRuleError) as conflict:
        engage_legacy_writer_fence(
            path,
            fence_id="fence-2",
            candidate_id="candidate-1",
            source_release="dish-42619b9",
            source_commit="42619b9",
            engaged_at=NOW,
            operator="Marco",
        )
    assert conflict.value.rule == "legacy_writer_fence_conflict"
    assert path.read_bytes() == committed_bytes
    with pytest.raises(DishRuleError) as release_conflict:
        release_legacy_writer_fence(path, expected_sha256="0" * 64)
    assert release_conflict.value.rule == "legacy_writer_fence_release_conflict"
    release_legacy_writer_fence(path, expected_sha256=digest)
    assert not path.exists()

    path.write_text("not-json", encoding="utf-8")
    unreadable, unreadable_digest = read_legacy_writer_fence(path)
    assert unreadable["format"] == "dish-legacy-writer-fence-unreadable-v1"
    assert len(unreadable_digest) == 64




def test_writer_fence_engagement_never_overwrites_a_concurrent_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "legacy-writer-fence.json"
    competitor = tmp_path / "competitor.json"
    engage_legacy_writer_fence(
        competitor,
        fence_id="fence-competitor",
        candidate_id="candidate-competitor",
        source_release="dish-competitor",
        source_commit="competitor",
        engaged_at=NOW,
        operator="Other",
    )
    competing_bytes = competitor.read_bytes()
    real_link = os.link
    raced = False

    def racing_link(src, dst, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            path.write_bytes(competing_bytes)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(fence_module.os, "link", racing_link)
    with pytest.raises(DishRuleError) as conflict:
        engage_legacy_writer_fence(
            path,
            fence_id="fence-planned",
            candidate_id="candidate-planned",
            source_release="dish-planned",
            source_commit="planned",
            engaged_at=NOW,
            operator="Marco",
        )
    assert conflict.value.rule == "legacy_writer_fence_conflict"
    assert path.read_bytes() == competing_bytes


@pytest.mark.parametrize("layout", ["immediate", "intermediate"])
def test_writer_fence_engagement_rejects_symlinked_parent_without_side_effects(
    tmp_path: Path, layout: str
) -> None:
    target = tmp_path / "symlink-target"
    target.mkdir()
    if layout == "immediate":
        governed = tmp_path / "governed"
        governed.symlink_to(target, target_is_directory=True)
        path = governed / "legacy-writer-fence.json"
        unexpected = target / path.name
    else:
        governed = tmp_path / "governed"
        governed.mkdir()
        linked = governed / "linked"
        linked.symlink_to(target, target_is_directory=True)
        path = linked / "created-through-link" / "legacy-writer-fence.json"
        unexpected = target / "created-through-link"

    with pytest.raises(DishRuleError) as rejected:
        engage_legacy_writer_fence(
            path,
            fence_id="fence-symlink-parent",
            candidate_id="candidate-symlink-parent",
            source_release="dish-42619b9",
            source_commit="42619b9",
            engaged_at=NOW,
            operator="Marco",
        )
    assert rejected.value.rule == "legacy_writer_fence_symlink_forbidden"
    assert not unexpected.exists()
    assert list(target.iterdir()) == []


def test_writer_fence_engagement_does_not_alter_target_through_parent_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "symlink-target"
    target.mkdir()
    target_file = target / "legacy-writer-fence.json"
    sentinel = b"existing target must remain untouched\n"
    target_file.write_bytes(sentinel)
    governed = tmp_path / "governed"
    governed.symlink_to(target, target_is_directory=True)

    with pytest.raises(DishRuleError):
        engage_legacy_writer_fence(
            governed / target_file.name,
            fence_id="fence-planned",
            candidate_id="candidate-planned",
            source_release="dish-planned",
            source_commit="planned",
            engaged_at=NOW,
            operator="Marco",
        )
    assert target_file.read_bytes() == sentinel

def test_writer_fence_observation_is_descriptor_bound_and_service_shaped(tmp_path: Path) -> None:
    path = tmp_path / "state" / "legacy-writer-fence.json"
    path.parent.mkdir()
    manifest, manifest_digest = engage_legacy_writer_fence(
        path,
        fence_id="fence-observed",
        candidate_id="candidate-observed",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    observation = observe_legacy_writer_fence(
        path,
        expected_path=path,
        expected_manifest_sha256=manifest_digest,
        expected_size=path.stat().st_size,
        clock=lambda: NOW,
    )
    payload = observation.as_evidence_payload()
    assert payload == {
        "format": "dish-writer-fence-observation-v1",
        "expected_path": str(path.resolve()),
        "observed_path": str(path.resolve()),
        "exists": True,
        "regular_file": True,
        "symlink_free": True,
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "manifest_sha256": manifest_digest,
        "filesystem_device": path.stat().st_dev,
        "filesystem_inode": path.stat().st_ino,
        "observed_size": path.stat().st_size,
        "stable": True,
        "identity_match": True,
        "observed_at": NOW.isoformat(),
    }
    assert manifest["fence_id"] == "fence-observed"


def test_writer_fence_observation_rejects_symlinks_and_wrong_types(tmp_path: Path) -> None:
    with pytest.raises(DishRuleError) as absent_error:
        observe_legacy_writer_fence(tmp_path / "absent.json")
    assert absent_error.value.rule == "legacy_writer_fence_absent"

    real = tmp_path / "real" / "legacy-writer-fence.json"
    real.parent.mkdir()
    _manifest, digest = engage_legacy_writer_fence(
        real,
        fence_id="fence-real",
        candidate_id="candidate-real",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    final_link = tmp_path / "final-link.json"
    final_link.symlink_to(real)
    with pytest.raises(DishRuleError) as final_error:
        observe_legacy_writer_fence(final_link, expected_manifest_sha256=digest)
    assert final_error.value.rule == "legacy_writer_fence_symlink_forbidden"

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real.parent, target_is_directory=True)
    with pytest.raises(DishRuleError) as parent_error:
        observe_legacy_writer_fence(parent_link / real.name, expected_manifest_sha256=digest)
    assert parent_error.value.rule == "legacy_writer_fence_symlink_forbidden"

    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with pytest.raises(DishRuleError) as type_error:
        observe_legacy_writer_fence(directory)
    assert type_error.value.rule == "legacy_writer_fence_type_invalid"


def test_writer_fence_observation_rejects_planned_identity_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "legacy-writer-fence.json"
    _manifest, digest = engage_legacy_writer_fence(
        path,
        fence_id="fence-identity",
        candidate_id="candidate-identity",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    with pytest.raises(DishRuleError) as path_error:
        observe_legacy_writer_fence(path, expected_path=tmp_path / "other.json")
    assert path_error.value.rule == "legacy_writer_fence_path_mismatch"

    with pytest.raises(DishRuleError) as digest_error:
        observe_legacy_writer_fence(path, expected_manifest_sha256="0" * 64)
    assert digest_error.value.rule == "legacy_writer_fence_identity_mismatch"
    assert digest_error.value.details["mismatches"]["manifest_sha256"] == {
        "planned": "0" * 64,
        "observed": digest,
    }

    with pytest.raises(DishRuleError) as inode_error:
        observe_legacy_writer_fence(path, expected_inode=path.stat().st_ino + 1)
    assert inode_error.value.rule == "legacy_writer_fence_identity_mismatch"


def test_writer_fence_observation_rejects_mid_read_change(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "legacy-writer-fence.json"
    _manifest, digest = engage_legacy_writer_fence(
        path,
        fence_id="fence-race",
        candidate_id="candidate-race",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    real_read = os.read
    changed = False

    def changing_read(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(fd, size)
        if chunk and not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b" ")
        return chunk

    monkeypatch.setattr(fence_module.os, "read", changing_read)
    with pytest.raises(DishRuleError) as unstable:
        observe_legacy_writer_fence(path, expected_manifest_sha256=digest)
    assert unstable.value.rule == "legacy_writer_fence_observation_unstable"


def test_unsafe_symlink_still_fences_legacy_mutation(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    engage_legacy_writer_fence(
        real,
        fence_id="fence-symlink",
        candidate_id="candidate-symlink",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    link = tmp_path / "link.json"
    link.symlink_to(real)
    unreadable, _digest = read_legacy_writer_fence(link)
    assert unreadable["format"] == "dish-legacy-writer-fence-unreadable-v1"
    with pytest.raises(DishRuleError) as fenced:
        fence_module.assert_legacy_writer_mutation_allowed(link)
    assert fenced.value.rule == "legacy_writer_fenced"

def test_http_fence_runs_after_authentication_and_before_body_parsing(tmp_path: Path) -> None:
    honest = tmp_path / "honest"
    honest.mkdir()
    fence_path = tmp_path / "legacy-writer-fence.json"
    engage_legacy_writer_fence(
        fence_path,
        fence_id="fence-1",
        candidate_id="candidate-1",
        source_release="dish-42619b9",
        source_commit="42619b9",
        engaged_at=NOW,
        operator="Marco",
    )
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            port=0,
            max_body_bytes=16,
            agent_token="cli-secret-1",
            admin_token="admin-secret-1",
            action_token="action-secret-1",
            legacy_writer_fence_path=fence_path,
        ),
        backend_factory=lambda: Backend(task_gid="123456789"),
        release_loader=_release_loader(honest),
    )
    server = build_server(service)
    thread = start_server_thread(server, daemon=True, name="stage6-fence-http")
    host, port = server.server_address
    parsed = urlsplit(f"http://{host}:{port}")
    try:
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "POST",
            "/v1/commands/start",
            body=b"{" + b"x" * 1000,
            headers={"Authorization": "Bearer wrong-token", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        unauthorized = json.loads(response.read())
        connection.close()
        assert response.status == 401
        assert unauthorized["errors"][0]["rule"] == "service_auth_invalid"

        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request(
            "POST",
            "/v1/commands/start",
            body=b"{" + b"x" * 1000,
            headers={"Authorization": "Bearer cli-secret-1", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        fenced = json.loads(response.read())
        connection.close()
        assert response.status == 409
        assert fenced["errors"][0]["rule"] == "legacy_writer_fenced"
    finally:
        stop_server(server, thread)
