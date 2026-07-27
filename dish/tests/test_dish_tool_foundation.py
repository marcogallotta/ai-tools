import json
import os
import socket
import sqlite3
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent
FIXTURE_RELEASE_DIR = Path(__file__).resolve().parent / "fixtures" / "dish-version-current"
sys.path.insert(0, str(BIN_DIR))
from dish_tool.backend import AsanaBackend, load_asana_pat, map_backend_exception  # noqa: E402
from dish_tool.constants import (  # noqa: E402
    ASANA_REQUEST_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS,
    EXIT_STATUS_BY_CODE,
    MAX_REQUEST_LIFETIME_SECONDS,
    NONTERMINAL_STATES,
    READ_TIMEOUT_SECONDS,
    RECOVERY_QUARANTINE_SECONDS,
    RECOVERY_SAFETY_MARGIN_SECONDS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
)
from dish_tool.database import (  # noqa: E402
    initialize_database,
    migrate_database,
    record_audit,
    transition_submission,
)
from dish_tool.errors import BackendFailure, DishRuleError, ReleaseResolutionError  # noqa: E402
from dish_tool.models import (  # noqa: E402
    RequestPhase,
    SectionRegistry,
    agent_family,
    is_protocol_managed,
    material_change_line,
    material_editor_line,
    opposite_family,
    resolve_destination,
)
from dish_tool.recovery import (  # noqa: E402
    begin_write_attempt,
    current_process_identity,
    finish_write_attempt,
    process_identity_is_live,
)
from dish_tool.releases import resolve_release  # noqa: E402
from dish_tool.results import exit_status, result_envelope  # noqa: E402
from dish_tool.validation import validate_note  # noqa: E402


PROTOCOLS = {
    "dish-planning-protocol.md": "# Planning protocol\nUse the planning manifest.\n",
    "dish-research-protocol.md": "# Research protocol\nBuild the complete task.\n",
    "dish-verification-protocol.md": "# Verification protocol\nVerify the complete task.\n",
}


def planning_manifest(version):
    return {
        "protocol_release": version,
        "manifest_kind": "planning",
        "headings": {
            "required": ["# PLANNING BRIEF"],
            "optional": [],
            "exactly_once": ["# PLANNING BRIEF"],
            "allowed": ["# PLANNING BRIEF"],
        },
        "labels": {
            "required": ["Destination section", "Exemptions"],
            "optional": ["Research notes"],
            "exactly_once": ["Destination section", "Exemptions"],
            "allowed": ["Destination section", "Exemptions", "Research notes"],
        },
        "contextual_labels": [],
        "exemptions": {
            "label": "Exemptions",
            "none_value": "None",
            "allowed_tags": [
                "nutrition-kcal",
                "nutrition-protein",
                "nutrition-fat",
            ],
        },
        "destination_section": {
            "label": "Destination section",
            "pattern": r"^(?P<name>[^()]+?)\s*\((?P<gid>[0-9]+)\)$",
        },
    }


def complete_manifest(version):
    return {
        "protocol_release": version,
        "manifest_kind": "complete_task",
        "headings": {
            "required": ["# DISH", "## PROCESS RECORD"],
            "optional": ["## QUANTITIES"],
            "exactly_once": ["# DISH", "## PROCESS RECORD"],
            "allowed": ["# DISH", "## QUANTITIES", "## PROCESS RECORD"],
        },
        "labels": {
            "required": [
                "Exemptions",
                "Destination section",
                "Self-verified",
                "Verification",
            ],
            "optional": ["Portions"],
            "exactly_once": [
                "Exemptions",
                "Destination section",
                "Self-verified",
                "Verification",
            ],
            "allowed": [
                "Exemptions",
                "Destination section",
                "Self-verified",
                "Verification",
                "Portions",
            ],
        },
        "contextual_labels": [
            {"heading": "## QUANTITIES", "required_label": "Portions"},
        ],
        "exemptions": planning_manifest(version)["exemptions"],
        "destination_section": planning_manifest(version)["destination_section"],
        "title": {
            "role_tags": [
                "side",
                "dessert",
                "component",
                "condiment",
                "benchmark",
                "comparison",
            ],
            "marker_pattern": r"^[^\[\]\r\n]+$",
            "marker_prefix": "[",
            "marker_suffix": "]",
            "separator": " — ",
            "unreviewed_blocker": "blockers unreviewed",
        },
    }


def run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def write_release(repo, version="fixture-v1", *, malformed=None, mismatch=None):
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "protocol_release").write_text(f"{version}\n")
    for name, content in PROTOCOLS.items():
        (repo / name).write_text(content.replace("protocol", f"protocol {version}", 1))
    manifests = {
        "dish-planning-manifest.json": planning_manifest(version),
        "dish-complete-task-manifest.json": complete_manifest(version),
    }
    if mismatch:
        manifests[mismatch]["protocol_release"] = "wrong-version"
    for name, content in manifests.items():
        if malformed == name:
            (repo / name).write_text("{not-json\n")
        else:
            (repo / name).write_text(
                json.dumps(content, indent=2, sort_keys=True) + "\n"
            )


def commit_release(repo, message="fixture release"):
    files = [
        "DISH_VERSION",
        "dish-task-schema.json",
        "dish-schema-migrations/0001-initial.json",
        "dish-planning-protocol.md",
        "dish-research-protocol.md",
        "dish-verification-protocol.md",
        "dish-cooking-protocol.md",
    ]
    run_git(repo, "add", *files)
    run_git(repo, "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "honest-pantry"
    shutil.copytree(FIXTURE_RELEASE_DIR, repo)
    run_git(repo, "init", "-b", "main")
    run_git(repo, "config", "user.name", "Fixture")
    run_git(repo, "config", "user.email", "fixture@example.invalid")
    commit = commit_release(repo)
    return repo, commit


def insert_submission(conn, submission_id, task_gid, status):
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, status, created_at
        ) VALUES (?, ?, 'planning', 'fixture-v1', 'abc', '{}', '{}',
                  'claude', 'claude', ?, '2026-07-21T00:00:00Z')
        """,
        (submission_id, task_gid, status),
    )
    conn.commit()


def test_schema_creation_and_migration_are_idempotent(tmp_path):
    db_path = tmp_path / "dish-tool.db"
    conn = initialize_database(db_path)
    migrate_database(conn)
    migrate_database(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"schema_migrations", "submissions", "audit_events"} <= tables

    submission_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(submissions)")
    }
    assert {
        "failed_verification_passes",
        "destination_section_name",
        "destination_section_gid",
        "write_attempt_id",
        "in_flight_at",
        "in_flight_hostname",
        "in_flight_pid",
        "in_flight_process_start",
        "research_queue_moved_at",
        "notes_written_at",
        "task_content_written_at",
        "destination_moved_at",
        "baseline_title",
        "baseline_title_fields",
        "prepared_title",
        "prepared_title_fields",
    } <= submission_columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_partial_unique_index_holds_for_every_nonterminal_state(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    for index, status in enumerate(sorted(NONTERMINAL_STATES)):
        task_gid = f"task-{index}"
        insert_submission(conn, f"first-{index}", task_gid, status)
        with pytest.raises(sqlite3.IntegrityError):
            insert_submission(conn, f"second-{index}", task_gid, "drafting")


def test_partial_unique_index_releases_for_terminal_states(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    for index, status in enumerate(sorted(TERMINAL_STATES)):
        task_gid = f"terminal-{index}"
        insert_submission(conn, f"old-{index}", task_gid, status)
        insert_submission(conn, f"new-{index}", task_gid, "drafting")


def test_resolver_loads_external_schema_adapter_for_legacy_note_checks(release_repo):
    repo, _ = release_repo
    release = resolve_release(repo, protocol_role="planning")

    assert release.protocol_version == "1.0.10"
    assert release.schema_version == "2"
    assert set(release.protocols) == {"planning"}
    assert set(release.manifests) == {"planning", "complete_task"}
    assert release.bundle_for_submission("planning") == {
        "planning": release.protocols["planning"]
    }


@pytest.mark.parametrize(
    ("agent", "family"),
    [("claude", "claude"), ("gpt", "gpt"), ("codex", "gpt")],
)
def test_agent_family_mapping(agent, family):
    assert agent_family(agent) == family
    assert opposite_family(family) != family


def test_unknown_agent_fails_closed():
    with pytest.raises(DishRuleError) as exc:
        agent_family("other")
    assert exc.value.code == "INVALID_ARGUMENT"


def test_task_provenance_uses_canonical_actor_names_and_separate_model_tokens():
    assert material_editor_line("gpt", "GPT-5.6 Thinking", "2026-07-27") == (
        "Custom GPT — GPT-5.6 Thinking, 2026-07-27"
    )
    assert material_editor_line("codex", "GPT-5.6-Codex", "2026-07-27") == (
        "Codex — GPT-5.6-Codex, 2026-07-27"
    )
    assert material_change_line(
        "gpt",
        "GPT-5.6 Thinking",
        "2026-07-27",
        change="adjusted the route",
        reason="the prior route was incomplete",
        materiality="Large",
    ) == (
        "2026-07-27 — Custom GPT — GPT-5.6 Thinking — adjusted the route — "
        "the prior route was incomplete — Large — pending-verification"
    )


def test_section_resolution_and_management_fail_closed():
    sections = [
        {"gid": "10", "name": "Research Queue"},
        {"gid": "11", "name": "Verification Queue"},
        {"gid": "12", "name": "Sourcing"},
        {"gid": "13", "name": "Reference"},
        {"gid": "14", "name": "Ready to Cook"},
    ]
    registry = SectionRegistry.from_sections(sections)

    assert is_protocol_managed("12", registry) is False
    assert is_protocol_managed("13", registry) is False
    assert is_protocol_managed("14", registry) is True
    assert is_protocol_managed(None, registry) is True
    assert resolve_destination("Ready to Cook", "14", registry).gid == "14"

    with pytest.raises(DishRuleError):
        resolve_destination("Research Queue", "10", registry)
    with pytest.raises(DishRuleError):
        resolve_destination("Ready to Cook", "999", registry)


def test_section_setup_rejects_missing_or_duplicate_names():
    sections = [
        {"gid": "10", "name": "Research Queue"},
        {"gid": "11", "name": "Verification Queue"},
        {"gid": "12", "name": "Sourcing"},
        {"gid": "13", "name": "Reference"},
        {"gid": "14", "name": "Reference"},
    ]
    with pytest.raises(DishRuleError):
        SectionRegistry.from_sections(sections)


def test_literal_note_validation_uses_manifest(release_repo):
    repo, _ = release_repo
    manifest = resolve_release(repo).manifests["planning"]
    valid = """# PLANNING BRIEF
Destination section: Ready to Cook (14)
Exemptions: [nutrition-kcal] Marco approved 2026-07-21 for this dish
Research notes: Opaque text
"""
    result = validate_note(valid, manifest)
    assert result.ok is True
    assert result.exemption_tags == ("nutrition-kcal",)
    assert result.destination_name == "Ready to Cook"
    assert result.destination_gid == "14"

    invalid = valid + "## UNKNOWN\nExemptions: None\n"
    result = validate_note(invalid, manifest)
    rules = {error["rule"] for error in result.errors}
    assert {"unknown_heading", "duplicate_label", "mixed_exemptions"} <= rules


def test_literal_note_validation_rejects_unknown_manifest_kind(release_repo):
    repo, _ = release_repo
    manifest = dict(resolve_release(repo).manifests["planning"])
    manifest["manifest_kind"] = "unknown"

    with pytest.raises(ReleaseResolutionError) as exc:
        validate_note("", manifest)
    assert exc.value.rule == "manifest_malformed"


def test_contextual_label_is_required_only_when_heading_present(release_repo):
    repo, _ = release_repo
    manifest = resolve_release(repo).manifests["complete_task"]
    note = """# DISH
Exemptions: None
Destination section: Ready to Cook (14)
Self-verified: claude, 2026-07-21
Verification: pending
## QUANTITIES
## PROCESS RECORD
"""
    result = validate_note(note, manifest)
    assert any(error["rule"] == "missing_contextual_label" for error in result.errors)


def test_common_result_contract_and_exit_statuses():
    success = result_envelope(command="prepare", state="ready")
    assert success == {
        "ok": True,
        "command": "prepare",
        "code": "OK",
        "task_gid": None,
        "submission_id": None,
        "state": "ready",
        "retryable": False,
        "allowed_actions": ["submit"],
        "data": {},
        "errors": [],
    }
    assert exit_status(success["code"]) == 0

    failure = result_envelope(
        command="prepare",
        ok=False,
        code="VALIDATION_FAILED",
        task_gid="t1",
        submission_id="s1",
        state="drafting",
        errors=[{"rule": "missing_label", "field": "Exemptions"}],
    )
    assert failure["retryable"] is True
    assert failure["allowed_actions"] == ["prepare"]
    assert exit_status(failure["code"]) == 2

    for code, expected in EXIT_STATUS_BY_CODE.items():
        assert exit_status(code) == expected


def test_audit_rows_allow_null_submission_and_keep_task_gid(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    event_id = record_audit(
        conn,
        submission_id=None,
        task_gid="task-123",
        event_type="generic_note_bypass",
        actor_agent="codex",
        details={"command": "set-notes", "code": "OK"},
    )
    row = conn.execute(
        "SELECT submission_id, task_gid, details FROM audit_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    assert row[0] is None
    assert row[1] == "task-123"
    assert json.loads(row[2]) == {"code": "OK", "command": "set-notes"}


def test_conditional_transition_fails_competing_state_change(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    insert_submission(conn, "s1", "t1", "drafting")

    transition_submission(conn, "s1", {"drafting"}, "ready")
    with pytest.raises(DishRuleError) as exc:
        transition_submission(conn, "s1", {"drafting"}, "ready")
    assert exc.value.code == "WRONG_STATE"


def test_process_identity_and_recovery_quarantine_invariant():
    identity = current_process_identity()
    assert identity.hostname == socket.gethostname()
    assert identity.pid == os.getpid()
    assert identity.process_start
    assert process_identity_is_live(identity) is True
    assert (
        RECOVERY_QUARANTINE_SECONDS
        > MAX_REQUEST_LIFETIME_SECONDS + RECOVERY_SAFETY_MARGIN_SECONDS
    )


def test_begin_write_attempt_records_identity_and_compare_and_swap(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    insert_submission(conn, "s1", "t1", "ready")

    attempt = begin_write_attempt(conn, "s1")
    row = conn.execute(
        """SELECT status, write_attempt_id, in_flight_hostname, in_flight_pid,
                  in_flight_process_start FROM submissions WHERE submission_id = 's1'"""
    ).fetchone()
    assert row[0] == "in_flight"
    assert row[1] == attempt.attempt_id
    assert row[2] == attempt.identity.hostname
    assert row[3] == attempt.identity.pid
    assert row[4] == attempt.identity.process_start

    with pytest.raises(DishRuleError):
        finish_write_attempt(
            conn,
            "s1",
            attempt_id="stale-attempt",
            target_state="written",
        )
    finish_write_attempt(
        conn,
        "s1",
        attempt_id=attempt.attempt_id,
        target_state="written",
    )
    assert (
        conn.execute(
            "SELECT status FROM submissions WHERE submission_id = 's1'"
        ).fetchone()[0]
        == "written"
    )


def test_resolver_preserves_requested_protocol_bytes_exactly(release_repo):
    repo, _ = release_repo
    exact = "# Research protocol\nTrailing spaces stay.   \n\n"
    (repo / "dish-research-protocol.md").write_text(exact)

    release = resolve_release(repo, protocol_role="research")
    assert release.protocols["research"] == exact


def test_backend_failure_classification_tracks_request_phase():
    pre_send = map_backend_exception(
        TimeoutError("connect failed"), phase=RequestPhase.PRE_SEND
    )
    assert pre_send.code == "BACKEND_REJECTED"
    assert pre_send.retryable is True

    possibly_sent = map_backend_exception(
        TimeoutError("response lost"), phase=RequestPhase.POSSIBLY_SENT
    )
    assert possibly_sent.code == "BACKEND_UNCERTAIN"
    assert possibly_sent.retryable is False

    class ServerError(Exception):
        status = 503
        body = "unavailable"
        reason = "Service Unavailable"

    server = map_backend_exception(ServerError(), phase=RequestPhase.RESPONSE_RECEIVED)
    assert server.code == "BACKEND_UNCERTAIN"
    assert server.status == 503

    class RequestTimeout(Exception):
        status = 408
        body = "request timeout"
        reason = "Request Timeout"

    timeout_response = map_backend_exception(
        RequestTimeout(), phase=RequestPhase.RESPONSE_RECEIVED
    )
    assert timeout_response.code == "BACKEND_UNCERTAIN"


def test_backend_call_without_explicit_tracker_marks_request_as_sent():
    backend = AsanaBackend(api_client=object())

    def fail_after_send(*args, **kwargs):
        raise TimeoutError("response lost")

    with pytest.raises(BackendFailure) as exc:
        backend.call(fail_after_send)
    assert exc.value.code == "BACKEND_UNCERTAIN"
    assert exc.value.phase == RequestPhase.POSSIBLY_SENT.value


def test_asana_backend_reuses_client_and_disables_sdk_retries(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "test-token")
    backend = AsanaBackend()
    try:
        first = backend.client()
        second = backend.client()

        assert first is second
        assert first.configuration.return_page_iterator is False
        assert first.configuration.retry_strategy.total == 0
    finally:
        backend.close()


def test_asana_backend_closes_only_the_client_it_created(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "test-token")
    owned = AsanaBackend()
    owned_client = owned.client()
    owned_pool = owned_client.pool

    owned.close()
    owned.close()

    assert owned_pool._state == "CLOSE"
    assert not owned_pool._terminate.still_active()
    with pytest.raises(DishRuleError) as exc:
        owned.client()
    assert exc.value.rule == "asana_backend_closed"

    class InjectedClient:
        def __init__(self):
            self.pool = type("Pool", (), {"close": lambda self: (_ for _ in ()).throw(AssertionError), "join": lambda self: (_ for _ in ()).throw(AssertionError)})()

    injected = InjectedClient()
    backend = AsanaBackend(api_client=injected)
    backend.close()
    assert injected.pool is not None


def test_asana_auth_loader_and_timeout_configuration(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ASANA_PAT=file-token\n")
    monkeypatch.delenv("ASANA_PAT", raising=False)
    monkeypatch.setenv("ASANA_ENV", str(env_file))
    assert load_asana_pat() == "file-token"

    monkeypatch.setenv("ASANA_PAT", "env-token")
    assert load_asana_pat() == "env-token"
    assert ASANA_REQUEST_TIMEOUT == (
        CONNECT_TIMEOUT_SECONDS,
        READ_TIMEOUT_SECONDS,
    )
