from __future__ import annotations

import hashlib
import json
import runpy
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)


def _namespace() -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts" / "dish-pg-release"))


def _sha(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_bundle_output_publishes_once_and_rejects_conflicting_authority(tmp_path: Path) -> None:
    namespace = _namespace()
    write_atomic = namespace["_write_atomic"]
    output = tmp_path / "bundle.json"
    payload = {"candidate": "one", "checks": ["a", "b"]}

    write_atomic(output, payload)
    first_inode = output.stat().st_ino
    write_atomic(output, payload)
    assert output.stat().st_ino == first_inode
    assert json.loads(output.read_text(encoding="utf-8")) == payload

    committed_bytes = output.read_bytes()
    with pytest.raises(ValueError, match="different content"):
        write_atomic(output, {"candidate": "two"})
    assert output.read_bytes() == committed_bytes

    output.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    output.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        write_atomic(output, payload)
    assert target.read_bytes() == b"{}\n"


@pytest.mark.parametrize("layout", ["immediate", "intermediate"])
def test_bundle_publication_rejects_symlinked_parent_without_side_effects(
    tmp_path: Path, layout: str
) -> None:
    namespace = _namespace()
    write_atomic = namespace["_write_atomic"]
    target = tmp_path / "symlink-target"
    target.mkdir()
    if layout == "immediate":
        governed = tmp_path / "governed"
        governed.symlink_to(target, target_is_directory=True)
        output = governed / "bundle.json"
        unexpected = target / output.name
    else:
        governed = tmp_path / "governed"
        governed.mkdir()
        linked = governed / "linked"
        linked.symlink_to(target, target_is_directory=True)
        output = linked / "created-through-link" / "bundle.json"
        unexpected = target / "created-through-link"

    with pytest.raises(ValueError, match="symbolic-link"):
        write_atomic(output, {"candidate": "planned"})
    assert not unexpected.exists()
    assert list(target.iterdir()) == []


def test_bundle_publication_does_not_alter_target_through_parent_symlink(
    tmp_path: Path,
) -> None:
    namespace = _namespace()
    write_atomic = namespace["_write_atomic"]
    target = tmp_path / "symlink-target"
    target.mkdir()
    target_file = target / "bundle.json"
    sentinel = b"existing target must remain untouched\n"
    target_file.write_bytes(sentinel)
    governed = tmp_path / "governed"
    governed.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic-link"):
        write_atomic(governed / target_file.name, {"candidate": "planned"})
    assert target_file.read_bytes() == sentinel


def test_bundle_database_commit_precedes_filesystem_publication(tmp_path: Path, monkeypatch) -> None:
    namespace = _namespace()
    bundle_call = namespace["_bundle_call"]
    globals_ = bundle_call.__globals__
    state = {"transaction_open": False, "disposed": False}
    candidate_id = uuid.uuid4()
    bundle_id = uuid.uuid4()
    manifest = {"format": "bundle", "candidate_id": str(candidate_id)}

    class Engine:
        def dispose(self) -> None:
            state["disposed"] = True

    @contextmanager
    def scope(_factory):
        state["transaction_open"] = True
        try:
            yield object()
        finally:
            state["transaction_open"] = False

    class Service:
        def __init__(self, _session) -> None:
            pass

        def build_evidence_bundle(self, **_kwargs):
            return SimpleNamespace(
                bundle_id=bundle_id,
                candidate_id=candidate_id,
                bundle_kind="release_candidate",
                manifest=manifest,
                manifest_sha256=_sha(manifest),
            )

    def publish(path: Path, payload: object) -> None:
        assert not state["transaction_open"]
        assert payload == manifest
        path.write_text("published", encoding="utf-8")

    monkeypatch.setitem(globals_, "create_database_engine", lambda _settings: Engine())
    monkeypatch.setitem(globals_, "session_factory", lambda _engine: object())
    monkeypatch.setitem(globals_, "session_scope", scope)
    monkeypatch.setitem(globals_, "ReleaseCandidateService", Service)
    monkeypatch.setitem(globals_, "_write_atomic", publish)
    monkeypatch.setitem(globals_, "_database_url", lambda: "sqlite+pysqlite:///:memory:")

    args = SimpleNamespace(
        candidate_id=str(candidate_id),
        kind="release_candidate",
        built_at=NOW.isoformat(),
        output=tmp_path / "bundle.json",
    )
    result, status = bundle_call(args)
    assert status == 0
    assert result["bundle_id"] == str(bundle_id)
    assert state["disposed"]


def test_bundle_ambiguous_commit_is_verified_before_publication(tmp_path: Path, monkeypatch) -> None:
    namespace = _namespace()
    bundle_call = namespace["_bundle_call"]
    globals_ = bundle_call.__globals__
    candidate_id = uuid.uuid4()
    bundle_id = uuid.uuid4()
    manifest = {"format": "bundle", "candidate_id": str(candidate_id)}
    snapshot = {
        "bundle_id": str(bundle_id),
        "candidate_id": str(candidate_id),
        "bundle_kind": "release_candidate",
        "manifest": manifest,
        "manifest_sha256": _sha(manifest),
    }
    published: list[object] = []

    class Engine:
        def dispose(self) -> None:
            pass

    @contextmanager
    def ambiguous_scope(_factory):
        yield object()
        raise RuntimeError("commit acknowledgement lost")

    class Service:
        def __init__(self, _session) -> None:
            pass

        def build_evidence_bundle(self, **_kwargs):
            return SimpleNamespace(**snapshot)

    monkeypatch.setitem(globals_, "create_database_engine", lambda _settings: Engine())
    monkeypatch.setitem(globals_, "session_factory", lambda _engine: object())
    monkeypatch.setitem(globals_, "session_scope", ambiguous_scope)
    monkeypatch.setitem(globals_, "ReleaseCandidateService", Service)
    monkeypatch.setitem(globals_, "_committed_bundle_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setitem(globals_, "_write_atomic", lambda _path, payload: published.append(payload))
    monkeypatch.setitem(globals_, "_database_url", lambda: "sqlite+pysqlite:///:memory:")

    result, status = bundle_call(
        SimpleNamespace(
            candidate_id=str(candidate_id),
            kind="release_candidate",
            built_at=NOW.isoformat(),
            output=tmp_path / "bundle.json",
        )
    )
    assert status == 0
    assert result["sha256"] == snapshot["manifest_sha256"]
    assert published == [manifest]


def test_bundle_definite_rollback_never_publishes(tmp_path: Path, monkeypatch) -> None:
    namespace = _namespace()
    bundle_call = namespace["_bundle_call"]
    globals_ = bundle_call.__globals__
    candidate_id = uuid.uuid4()
    manifest = {"format": "bundle"}
    published: list[object] = []

    class Engine:
        def dispose(self) -> None:
            pass

    @contextmanager
    def failed_scope(_factory):
        yield object()
        raise RuntimeError("commit failed")

    class Service:
        def __init__(self, _session) -> None:
            pass

        def build_evidence_bundle(self, **_kwargs):
            return SimpleNamespace(
                bundle_id=uuid.uuid4(),
                candidate_id=candidate_id,
                bundle_kind="release_candidate",
                manifest=manifest,
                manifest_sha256=_sha(manifest),
            )

    monkeypatch.setitem(globals_, "create_database_engine", lambda _settings: Engine())
    monkeypatch.setitem(globals_, "session_factory", lambda _engine: object())
    monkeypatch.setitem(globals_, "session_scope", failed_scope)
    monkeypatch.setitem(globals_, "ReleaseCandidateService", Service)
    monkeypatch.setitem(globals_, "_committed_bundle_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_write_atomic", lambda _path, payload: published.append(payload))
    monkeypatch.setitem(globals_, "_database_url", lambda: "sqlite+pysqlite:///:memory:")

    with pytest.raises(RuntimeError, match="commit failed"):
        bundle_call(
            SimpleNamespace(
                candidate_id=str(candidate_id),
                kind="release_candidate",
                built_at=NOW.isoformat(),
                output=tmp_path / "bundle.json",
            )
        )
    assert published == []


def test_writer_fence_filesystem_work_occurs_between_database_transactions(
    tmp_path: Path, monkeypatch
) -> None:
    namespace = _namespace()
    engage_call = namespace["_engage_fence_call"]
    globals_ = engage_call.__globals__
    fence_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    order: list[str] = []

    class Engine:
        def dispose(self) -> None:
            order.append("dispose")

    def context(_factory, requested_fence_id):
        assert requested_fence_id == fence_id
        order.append("database-intent")
        return {
            "fence_id": str(fence_id),
            "candidate_id": str(candidate_id),
            "generation_id": str(uuid.uuid4()),
            "source_release": "dish-release",
            "source_commit": "commit",
            "database_state": "prepared",
        }

    def engage(path: Path, **_kwargs):
        order.append("filesystem-engage-fsync")
        path.write_text("{}\n", encoding="utf-8")
        return {}, "a" * 64

    observation = SimpleNamespace(
        observed_path=str(tmp_path / "fence.json"),
        artifact_sha256="b" * 64,
        device=12,
        inode=34,
        observed_at=NOW,
        as_evidence_payload=lambda: {"stable": True},
    )

    def record(_factory, **kwargs):
        assert order == ["database-intent", "filesystem-engage-fsync", "observe"]
        assert kwargs["artifact_generation_identity"]
        assert kwargs["observation"] is observation
        order.append("database-completion")
        return {
            "fence_id": str(fence_id),
            "state": "engaged",
            "artifact_observation_id": str(uuid.uuid4()),
        }

    monkeypatch.setitem(globals_, "create_database_engine", lambda _settings: Engine())
    monkeypatch.setitem(globals_, "session_factory", lambda _engine: object())
    monkeypatch.setitem(globals_, "_database_url", lambda: "sqlite+pysqlite:///:memory:")
    monkeypatch.setitem(globals_, "_fence_context", context)
    monkeypatch.setitem(globals_, "engage_legacy_writer_fence", engage)
    monkeypatch.setitem(
        globals_,
        "observe_legacy_writer_fence",
        lambda *_args, **_kwargs: order.append("observe") or observation,
    )
    monkeypatch.setitem(globals_, "_record_fence_engaged", record)

    result, status = engage_call(
        SimpleNamespace(
            fence_id=str(fence_id),
            path=tmp_path / "fence.json",
            operator="Marco",
            engaged_at=NOW.isoformat(),
        )
    )
    assert status == 0
    assert result["state"] == "engaged"
    assert order == [
        "database-intent",
        "filesystem-engage-fsync",
        "observe",
        "database-completion",
        "dispose",
    ]


def test_writer_fence_split_state_is_explicitly_retryable(tmp_path: Path, monkeypatch) -> None:
    namespace = _namespace()
    engage_call = namespace["_engage_fence_call"]
    globals_ = engage_call.__globals__
    fence_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    contexts = iter(["prepared", "prepared"])

    class Engine:
        def dispose(self) -> None:
            pass

    def context(_factory, _fence_id):
        return {
            "fence_id": str(fence_id),
            "candidate_id": str(candidate_id),
            "generation_id": str(uuid.uuid4()),
            "source_release": "dish-release",
            "source_commit": "commit",
            "database_state": next(contexts),
        }

    observation = SimpleNamespace(
        observed_path=str(tmp_path / "fence.json"),
        artifact_sha256="b" * 64,
        device=12,
        inode=34,
        observed_at=NOW,
        as_evidence_payload=lambda: {"stable": True},
    )
    monkeypatch.setitem(globals_, "create_database_engine", lambda _settings: Engine())
    monkeypatch.setitem(globals_, "session_factory", lambda _engine: object())
    monkeypatch.setitem(globals_, "_database_url", lambda: "sqlite+pysqlite:///:memory:")
    monkeypatch.setitem(globals_, "_fence_context", context)
    monkeypatch.setitem(
        globals_, "engage_legacy_writer_fence", lambda *_args, **_kwargs: ({}, "a" * 64)
    )
    monkeypatch.setitem(globals_, "observe_legacy_writer_fence", lambda *_args, **_kwargs: observation)
    monkeypatch.setitem(
        globals_,
        "_record_fence_engaged",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("commit failed")),
    )

    with pytest.raises(RuntimeError, match="retry this exact command"):
        engage_call(
            SimpleNamespace(
                fence_id=str(fence_id),
                path=tmp_path / "fence.json",
                operator="Marco",
                engaged_at=NOW.isoformat(),
            )
        )


def test_writer_fence_completion_persists_observation_before_engagement(monkeypatch) -> None:
    namespace = _namespace()
    record = namespace["_record_fence_engaged"]
    globals_ = record.__globals__
    fence_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    calls: list[tuple[str, object]] = []

    @contextmanager
    def scope(_factory):
        yield object()

    class Service:
        def __init__(self, _session) -> None:
            pass

        def record_writer_fence_artifact_observation(self, **kwargs):
            calls.append(("observe", kwargs))
            return SimpleNamespace(observation_id=observation_id)

        def engage_writer_fence(self, **kwargs):
            calls.append(("engage", kwargs))
            assert kwargs["artifact_observation_id"] == observation_id
            return SimpleNamespace(fence_id=fence_id, state="engaged")

    observation = SimpleNamespace(
        observed_path="/srv/dish/writer-fence.json",
        artifact_sha256="a" * 64,
        device=12,
        inode=34,
        observed_at=NOW,
    )
    monkeypatch.setitem(globals_, "session_scope", scope)
    monkeypatch.setitem(globals_, "ReleaseCandidateService", Service)

    result = record(
        object(),
        fence_id=fence_id,
        artifact_generation_identity="generation-1",
        observation=observation,
        engaged_at=NOW,
    )

    assert result["artifact_observation_id"] == str(observation_id)
    assert [name for name, _payload in calls] == ["observe", "engage"]
    persisted = calls[0][1]
    assert persisted["canonical_path"] == observation.observed_path
    assert persisted["content_sha256"] == observation.artifact_sha256
    assert persisted["verification_result"] == "matched"
