from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from implementation_claim_lib.client import ClaimServiceClient
from implementation_claim_lib.errors import ClaimError
from implementation_claim_lib.orchestration import AsanaMirror
from implementation_claim_lib.service import ClaimCoordinator
from implementation_claim_lib.store import ClaimStore

REPO = "marcogallotta/ai-tools"
BASE = "1" * 40
HEAD1 = "2" * 40
HEAD2 = "3" * 40


class Mirror:
    def __init__(self) -> None:
        self.fail = False
        self.synced: list[dict[str, Any]] = []

    def sync(self, claim: dict[str, Any]) -> None:
        self.synced.append(dict(claim))
        if self.fail:
            raise ClaimError("ASANA_UNAVAILABLE", "fixture outage", 503)


class GitHub:
    def __init__(self) -> None:
        self.heads: dict[str, str | None] = {}

    def branch_head(self, repository: str, branch: str) -> str | None:
        assert repository == REPO
        return self.heads.get(branch)


class FakeAsanaMirror(AsanaMirror):
    def __init__(self, *, ignore_moves: bool = False) -> None:
        super().__init__(token="fixture", allowed_projects=frozenset({"project-1"}))
        self.ignore_moves = ignore_moves
        self.task: dict[str, Any] = {
            "gid": "1217463105325599",
            "name": "claim task",
            "completed": False,
            "memberships": [{
                "project": {"gid": "project-1", "name": "Workflow"},
                "section": {"gid": "ready", "name": "Ready"},
            }],
        }
        self.sections = [
            {"gid": "ready", "name": "Ready"},
            {"gid": "active", "name": "In Progress"},
            {"gid": "review", "name": "Review / Integration"},
        ]
        self.stories: list[dict[str, Any]] = []

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method == "GET" and path.startswith("/tasks/") and "/stories" not in path:
            return {**self.task, "memberships": [dict(m, section=dict(m["section"])) for m in self.task["memberships"]]}
        if method == "GET" and path.startswith("/projects/") and "/sections" in path:
            return list(self.sections)
        if method == "GET" and "/stories" in path:
            return list(self.stories)
        if method == "POST" and path.startswith("/sections/") and path.endswith("/addTask"):
            if not self.ignore_moves:
                section_gid = path.split("/")[2]
                section = next(item for item in self.sections if item["gid"] == section_gid)
                self.task["memberships"][0]["section"] = dict(section)
            return {}
        if method == "POST" and path.endswith("/stories"):
            assert body is not None
            self.stories.append({"gid": f"story-{len(self.stories)+1}", "text": body["data"]["text"]})
            return self.stories[-1]
        raise AssertionError((method, path, body))


def coordinator(path: Path, mirror: Mirror | None = None, github: GitHub | None = None) -> ClaimCoordinator:
    return ClaimCoordinator(ClaimStore(path), repository=REPO, asana=mirror or Mirror(), github=github)


def acquire(c: ClaimCoordinator, task: str = "1217463105325599", *, owner: str = "a", branch: str = "agent/global") -> dict[str, Any]:
    return c.acquire({
        "repository": REPO,
        "task_gid": task,
        "owner": owner,
        "session_id": f"session-{owner}",
        "host": f"host-{owner}",
        "authoring_base_sha": BASE,
        "branch": branch,
    })


def error_code(exc: BaseException) -> str:
    assert isinstance(exc, ClaimError)
    return exc.code


def test_three_cross_host_acquires_exactly_one_wins(tmp_path: Path) -> None:
    db = tmp_path / "claims.sqlite3"
    coordinators = [coordinator(db) for _ in range(3)]
    barrier = threading.Barrier(3)
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        try:
            claim = acquire(coordinators[index], owner=str(index))
            result = ("ok", claim["claim_id"])
        except ClaimError as exc:
            result = ("err", exc.code)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sum(kind == "ok" for kind, _ in results) == 1
    assert [value for kind, value in results if kind == "err"] == ["OWNERSHIP_CONFLICT", "OWNERSHIP_CONFLICT"]


def test_sync_failure_fences_writes_and_dispatch_until_reconciled(tmp_path: Path) -> None:
    mirror = Mirror(); mirror.fail = True
    c = coordinator(tmp_path / "claims.sqlite3", mirror)
    with pytest.raises(ClaimError) as caught:
        acquire(c)
    assert caught.value.code == "ASANA_UNAVAILABLE"
    pending = caught.value.current
    capability = caught.value.writer_capability
    assert pending is not None and pending["asana_sync_state"] == "pending"
    assert isinstance(capability, str) and capability
    assert c.dispatch_guard(pending["task_gid"])["dispatchable"] is False
    with pytest.raises(ClaimError) as second:
        acquire(c, owner="b")
    assert second.value.code == "OWNERSHIP_CONFLICT"
    with pytest.raises(ClaimError) as auth:
        c.authorize({"repository": REPO, "task_gid": pending["task_gid"], "claim_id": pending["claim_id"], "writer_capability": capability, "branch": "agent/global"})
    assert auth.value.code == "ORCHESTRATION_SYNC_PENDING"

    mirror.fail = False
    synced = c.sync({"repository": REPO, "task_gid": pending["task_gid"], "claim_id": pending["claim_id"], "writer_capability": capability})
    assert synced["writable"] is True
    assert c.authorize({"repository": REPO, "task_gid": pending["task_gid"], "claim_id": pending["claim_id"], "writer_capability": capability, "branch": "agent/global"})["claim_id"] == pending["claim_id"]


def test_exact_generation_takeover_is_aba_safe_and_old_owner_is_fenced(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    first = acquire(c)
    second = c.takeover({
        "repository": REPO,
        "task_gid": first["task_gid"],
        "expected_claim_id": first["claim_id"],
        "owner": "b",
        "session_id": "session-b",
        "host": "host-b",
        "authoring_base_sha": BASE,
        "reason": "explicit replacement",
        "liveness_evidence": "coordinator declared prior owner stale",
    }, recovery_authorized=True)
    assert second["generation"] == 2 and second["claim_id"] != first["claim_id"]
    with pytest.raises(ClaimError) as stale:
        c.authorize({"repository": REPO, "task_gid": first["task_gid"], "claim_id": first["claim_id"], "writer_capability": first["writer_capability"], "branch": "agent/global"})
    assert stale.value.code == "OWNERSHIP_CONFLICT"
    with pytest.raises(ClaimError) as aba:
        c.takeover({
            "repository": REPO,
            "task_gid": first["task_gid"],
            "expected_claim_id": first["claim_id"],
            "owner": "c", "session_id": "session-c", "host": "host-c", "authoring_base_sha": BASE,
            "reason": "racing replacement", "liveness_evidence": "same stale observation",
        }, recovery_authorized=True)
    assert aba.value.code == "OWNERSHIP_CONFLICT"


def test_two_concurrent_takeovers_against_same_generation_exactly_one_wins(tmp_path: Path) -> None:
    db = tmp_path / "claims.sqlite3"
    first = acquire(coordinator(db))
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def run(owner: str) -> None:
        c = coordinator(db)
        barrier.wait()
        try:
            c.takeover({
                "repository": REPO, "task_gid": first["task_gid"], "expected_claim_id": first["claim_id"],
                "owner": owner, "session_id": f"session-{owner}", "host": f"host-{owner}",
                "authoring_base_sha": BASE, "reason": "explicit recovery", "liveness_evidence": "bounded evidence",
            }, recovery_authorized=True)
            result = "ok"
        except ClaimError as exc:
            result = exc.code
        with lock:
            results.append(result)

    threads = [threading.Thread(target=run, args=(owner,)) for owner in ("b", "c")]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=10)
    assert sorted(results) == ["OWNERSHIP_CONFLICT", "ok"]


def test_branch_and_pr_lineage_cannot_be_adopted_by_another_task(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    first = acquire(c, task="1217463105325599", branch="agent/shared")
    first = c.bind_pr({"repository": REPO, "task_gid": first["task_gid"], "claim_id": first["claim_id"], "writer_capability": first["writer_capability"], "pr_number": 90, "pr_head": HEAD1})
    second = acquire(c, task="1217463105325600", owner="b", branch="agent/other")
    with pytest.raises(ClaimError) as branch:
        c.bind_branch({"repository": REPO, "task_gid": second["task_gid"], "claim_id": second["claim_id"], "writer_capability": second["writer_capability"], "branch": "agent/shared"})
    assert branch.value.code == "LINEAGE_CONFLICT"
    with pytest.raises(ClaimError) as pr:
        c.bind_pr({"repository": REPO, "task_gid": second["task_gid"], "claim_id": second["claim_id"], "writer_capability": second["writer_capability"], "pr_number": 90, "pr_head": HEAD1})
    assert pr.value.code == "LINEAGE_CONFLICT"


def test_wrong_claim_id_cannot_begin_publication_even_with_correct_head(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    claim = acquire(c)
    with pytest.raises(ClaimError) as caught:
        c.begin_publication({
            "repository": REPO, "task_gid": claim["task_gid"], "claim_id": "deadbeef" * 4, "writer_capability": claim["writer_capability"],
            "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "request-1",
        })
    assert caught.value.code == "OWNERSHIP_CONFLICT"
    assert c.store.publication(REPO, claim["task_gid"], "request-1") is None
    assert c.status(claim["task_gid"])["branch_head"] is None


def test_stale_claim_cannot_complete_losing_local_candidate_after_takeover(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    first = acquire(c)
    second = c.takeover({
        "repository": REPO, "task_gid": first["task_gid"], "expected_claim_id": first["claim_id"],
        "owner": "b", "session_id": "session-b", "host": "host-b", "authoring_base_sha": BASE,
        "reason": "replacement", "liveness_evidence": "bounded evidence",
    }, recovery_authorized=True)
    with pytest.raises(ClaimError) as caught:
        c.begin_publication({
            "repository": REPO, "task_gid": first["task_gid"], "claim_id": first["claim_id"], "writer_capability": first["writer_capability"],
            "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "loser",
        })
    assert caught.value.code == "OWNERSHIP_CONFLICT"
    assert c.status(first["task_gid"])["claim_id"] == second["claim_id"]
    assert c.store.publication(REPO, first["task_gid"], "loser") is None


def test_publication_journal_reconciles_crash_after_branch_move(tmp_path: Path) -> None:
    github = GitHub()
    c = coordinator(tmp_path / "claims.sqlite3", github=github)
    claim = acquire(c)
    begin = c.begin_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "publish-1",
    })
    assert begin["publication"]["state"] == "pending"
    github.heads["agent/global"] = HEAD1
    reconciled = c.reconcile_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"], "request_id": "publish-1",
    })
    assert reconciled["reconciled"] is True
    assert reconciled["claim"]["branch_head"] == HEAD1
    assert reconciled["publication"]["state"] == "completed"


def test_publication_idempotency_and_head_cas(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    claim = acquire(c)
    first = c.begin_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "r",
    })
    replay = c.begin_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "r",
    })
    assert replay["replay"] is True and replay["publication"]["request_digest"] == first["publication"]["request_digest"]
    c.complete_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "request_id": "r", "result_head": HEAD1,
    })
    with pytest.raises(ClaimError) as moved:
        c.begin_publication({
            "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
            "branch": "agent/global", "expected_head": None, "proposed_head": HEAD2, "request_id": "r2",
        })
    assert moved.value.code == "HEAD_MOVED"


def test_review_ready_binds_exact_pr_head_and_stale_release_fails(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    claim = acquire(c)
    begin = c.begin_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "publish",
    })
    assert begin["claim"]["state"] == "publishing"
    completed = c.complete_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "request_id": "publish", "result_head": HEAD1, "pr_number": 91,
    })
    ready = c.review_ready({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "pr_number": 91, "pr_head": HEAD1,
    })
    assert ready["state"] == "review-ready" and ready["pr_head"] == HEAD1
    replacement = c.takeover({
        "repository": REPO, "task_gid": claim["task_gid"], "expected_claim_id": claim["claim_id"],
        "owner": "b", "session_id": "session-b", "host": "host-b", "authoring_base_sha": BASE,
        "reason": "review blocked; explicit fix takeover", "liveness_evidence": "review dispatcher handoff",
    }, recovery_authorized=True)
    assert replacement["pr_number"] == completed["claim"]["pr_number"] == 91
    with pytest.raises(ClaimError) as stale:
        c.release({"repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"], "reason": "old owner woke"})
    assert stale.value.code == "OWNERSHIP_CONFLICT"

def test_asana_sync_moves_ready_and_records_exact_generation_marker(tmp_path: Path) -> None:
    mirror = FakeAsanaMirror()
    c = coordinator(tmp_path / "claims.sqlite3", mirror=mirror)
    claim = acquire(c)
    assert mirror.task["memberships"][0]["section"]["name"] == "In Progress"
    marker = AsanaMirror.marker(claim)
    assert any(story["text"] == marker for story in mirror.stories)
    assert claim["asana_sync_state"] == "synced"


def test_asana_readback_failure_keeps_claim_fenced_and_non_dispatchable(tmp_path: Path) -> None:
    mirror = FakeAsanaMirror(ignore_moves=True)
    c = coordinator(tmp_path / "claims.sqlite3", mirror=mirror)
    with pytest.raises(ClaimError) as caught:
        acquire(c)
    assert caught.value.code == "ASANA_SYNC_VERIFY_FAILED"
    claim = caught.value.current
    assert claim is not None and claim["asana_sync_state"] == "pending"
    assert c.dispatch_guard(claim["task_gid"])["dispatchable"] is False


def test_unresolved_publication_blocks_second_intent_and_takeover(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    claim = acquire(c)
    c.begin_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
        "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "first",
    })
    with pytest.raises(ClaimError) as second_publish:
        c.begin_publication({
            "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"],
            "branch": "agent/global", "expected_head": None, "proposed_head": HEAD2, "request_id": "second",
        })
    assert second_publish.value.code == "PUBLICATION_PENDING"
    with pytest.raises(ClaimError) as takeover:
        c.takeover({
            "repository": REPO, "task_gid": claim["task_gid"], "expected_claim_id": claim["claim_id"],
            "owner": "b", "session_id": "session-b", "host": "host-b", "authoring_base_sha": BASE,
            "reason": "replacement", "liveness_evidence": "owner unresponsive",
        }, recovery_authorized=True)
    assert takeover.value.code == "PUBLICATION_PENDING"
    aborted = c.abort_publication({
        "repository": REPO, "task_gid": claim["task_gid"], "claim_id": claim["claim_id"], "writer_capability": claim["writer_capability"], "request_id": "first",
    })
    assert aborted["state"] == "claimed"
    replacement = c.takeover({
        "repository": REPO, "task_gid": claim["task_gid"], "expected_claim_id": claim["claim_id"],
        "owner": "b", "session_id": "session-b", "host": "host-b", "authoring_base_sha": BASE,
        "reason": "replacement", "liveness_evidence": "owner unresponsive and pending intent aborted",
    }, recovery_authorized=True)
    assert replacement["generation"] == claim["generation"] + 1


def test_public_generation_never_carries_private_writer_authority(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    winner = acquire(c)
    public = c.status(winner["task_gid"])
    assert public is not None
    assert public["claim_id"] == winner["claim_id"]
    assert "writer_capability" not in public
    assert "writer_capability_hash" not in public
    with pytest.raises(ClaimError) as conflict:
        acquire(c, owner="loser")
    assert conflict.value.code == "OWNERSHIP_CONFLICT"
    assert conflict.value.current is not None
    assert "writer_capability" not in conflict.value.current
    assert "writer_capability_hash" not in conflict.value.current
    assert conflict.value.writer_capability is None


def test_observer_with_public_claim_id_cannot_mutate_or_self_promote(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    winner = acquire(c)
    public = c.status(winner["task_gid"])
    assert public is not None
    forged = "observer-does-not-own-this-generation" * 2

    attempts = [
        lambda: c.authorize({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged, "branch": "agent/global"}),
        lambda: c.renew({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged}),
        lambda: c.bind_branch({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged, "branch": "agent/global"}),
        lambda: c.bind_pr({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged, "pr_number": 99, "pr_head": HEAD1}),
        lambda: c.begin_publication({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged, "branch": "agent/global", "expected_head": None, "proposed_head": HEAD1, "request_id": "forged"}),
        lambda: c.release({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged, "reason": "forged release"}),
        lambda: c.supersede({"repository": REPO, "task_gid": winner["task_gid"], "claim_id": public["claim_id"], "writer_capability": forged, "reason": "forged supersede"}),
    ]
    for attempt in attempts:
        with pytest.raises(ClaimError) as denied:
            attempt()
        assert denied.value.code == "WRITER_AUTHORITY_DENIED"

    with pytest.raises(ClaimError) as takeover:
        c.takeover({
            "repository": REPO, "task_gid": winner["task_gid"], "expected_claim_id": public["claim_id"],
            "owner": "observer", "session_id": "observer-session", "host": "observer-host",
            "authoring_base_sha": BASE, "reason": "self promotion", "liveness_evidence": "free-form claim",
        })
    assert takeover.value.code == "RECOVERY_AUTHORITY_REQUIRED"
    current = c.status(winner["task_gid"])
    assert current is not None and current["claim_id"] == winner["claim_id"]
    assert c.store.publication(REPO, winner["task_gid"], "forged") is None


def test_winner_capability_works_and_authorized_takeover_rotates_it(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    first = acquire(c)
    assert c.authorize({
        "repository": REPO, "task_gid": first["task_gid"], "claim_id": first["claim_id"],
        "writer_capability": first["writer_capability"], "branch": "agent/global",
    })["claim_id"] == first["claim_id"]

    second = c.takeover({
        "repository": REPO, "task_gid": first["task_gid"], "expected_claim_id": first["claim_id"],
        "owner": "replacement", "session_id": "replacement-session", "host": "replacement-host",
        "authoring_base_sha": BASE, "reason": "explicit recovery authority handoff",
        "liveness_evidence": "bounded orchestrator evidence",
    }, recovery_authorized=True)
    assert second["claim_id"] != first["claim_id"]
    assert second["writer_capability"] != first["writer_capability"]

    with pytest.raises(ClaimError) as old_generation:
        c.authorize({
            "repository": REPO, "task_gid": first["task_gid"], "claim_id": first["claim_id"],
            "writer_capability": first["writer_capability"], "branch": "agent/global",
        })
    assert old_generation.value.code == "OWNERSHIP_CONFLICT"

    for action in (
        lambda: c.authorize({"repository": REPO, "task_gid": second["task_gid"], "claim_id": second["claim_id"], "writer_capability": first["writer_capability"], "branch": "agent/global"}),
        lambda: c.release({"repository": REPO, "task_gid": second["task_gid"], "claim_id": second["claim_id"], "writer_capability": first["writer_capability"], "reason": "stale writer"}),
        lambda: c.supersede({"repository": REPO, "task_gid": second["task_gid"], "claim_id": second["claim_id"], "writer_capability": first["writer_capability"], "reason": "stale writer"}),
    ):
        with pytest.raises(ClaimError) as stale_capability:
            action()
        assert stale_capability.value.code == "WRITER_AUTHORITY_DENIED"

    assert c.authorize({
        "repository": REPO, "task_gid": second["task_gid"], "claim_id": second["claim_id"],
        "writer_capability": second["writer_capability"], "branch": "agent/global",
    })["claim_id"] == second["claim_id"]


def test_ordinary_service_client_cannot_call_recovery_takeover_without_recovery_credential() -> None:
    client = ClaimServiceClient(
        url="http://127.0.0.1:9", token="ordinary-shared-token", repository=REPO, recovery_token=None
    )
    with pytest.raises(ClaimError) as caught:
        client.takeover(
            task_gid="1217463105325599", expected_claim_id="public-generation", owner="loser",
            session_id="loser-session", host="loser-host", authoring_base_sha=BASE,
            reason="self promotion", liveness_evidence="free-form text",
        )
    assert caught.value.code == "RECOVERY_AUTHORITY_REQUIRED"


def test_pre_capability_generation_fails_closed_until_authorized_takeover(tmp_path: Path) -> None:
    c = coordinator(tmp_path / "claims.sqlite3")
    first = acquire(c)
    with c.store._write() as conn:
        conn.execute(
            "UPDATE implementation_claims SET schema_version=1, writer_capability_hash=NULL WHERE repository=? AND task_gid=?",
            (REPO, first["task_gid"]),
        )
    with pytest.raises(ClaimError) as denied:
        c.authorize({
            "repository": REPO, "task_gid": first["task_gid"], "claim_id": first["claim_id"],
            "writer_capability": first["writer_capability"], "branch": "agent/global",
        })
    assert denied.value.code == "WRITER_AUTHORITY_DENIED"
    replacement = c.takeover({
        "repository": REPO, "task_gid": first["task_gid"], "expected_claim_id": first["claim_id"],
        "owner": "recovery", "session_id": "recovery-session", "host": "recovery-host",
        "authoring_base_sha": BASE, "reason": "schema-v2 authority upgrade",
        "liveness_evidence": "explicit recovery path owns the migration",
    }, recovery_authorized=True)
    assert replacement["schema_version"] == 2
    assert replacement["writer_capability"]
    assert c.authorize({
        "repository": REPO, "task_gid": replacement["task_gid"], "claim_id": replacement["claim_id"],
        "writer_capability": replacement["writer_capability"], "branch": "agent/global",
    })["claim_id"] == replacement["claim_id"]
