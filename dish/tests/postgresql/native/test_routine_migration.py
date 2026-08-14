from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from dish_pg import migrate
from dish_pg.migrate import main
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.shadow_worker_entrypoint import main as shadow_worker_main
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]
ROOT = Path(__file__).resolve().parents[3]
PREVIOUS_HEAD = "0038_cutover_rehearsal_identity"


def _source_commit() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _database_name(dsn: str) -> str:
    engine = create_engine(dsn, future=True)
    try:
        with engine.connect() as connection:
            return str(connection.scalar(text("SELECT current_database()")))
    finally:
        engine.dispose()


def _heads(dsn: str) -> tuple[str, ...]:
    engine = create_engine(dsn, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))
            return tuple(str(row[0]) for row in rows)
    finally:
        engine.dispose()


def _reset_to_revision(dsn: str, revision: str) -> None:
    engine = create_engine(dsn, future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            connection.exec_driver_sql("CREATE SCHEMA public")
    finally:
        engine.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(cfg, revision)


def _rewrite_heads(dsn: str, *revisions: str) -> None:
    engine = create_engine(dsn, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM alembic_version"))
            for revision in revisions:
                connection.execute(
                    text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
                    {"revision": revision},
                )
    finally:
        engine.dispose()


def _invoke(tmp_path: Path, dsn: str, *, mode: str, environment: str = "test", expected: str | None = None, confirmation: str | None = None, evidence_name: str = "migration.json") -> tuple[int, dict]:
    expected_name = expected or _database_name(dsn)
    argv = [
        "--environment", environment,
        "--database-url", dsn,
        "--expected-database-name", expected_name,
        "--source-commit", _source_commit(),
        "--evidence-file", str(tmp_path / evidence_name),
        f"--{mode}",
    ]
    if confirmation is not None:
        argv.extend(["--confirm-database-name", confirmation])
    status = main(argv)
    records = [
        json.loads(line)
        for line in (tmp_path / evidence_name).read_text(encoding="utf-8").splitlines()
    ]
    return status, records[-1]


def _shadow_args(dsn: str, expected: str, tmp_path: Path) -> list[str]:
    return [
        "--database-url", dsn,
        "--expected-database-name", expected,
        "--spool-path", str(tmp_path / "unused-spool.sqlite3"),
        "--baseline-id", str(uuid.UUID(int=1)),
        "--worker-id", "native-startup-cert",
        "--cursor-secret-file", str(tmp_path / "unused-secret"),
        "--comparator-release", "native-startup-cert",
        "--kill-switch", str(tmp_path / "unused-kill-switch"),
    ]


def test_one_revision_behind_applies_exact_expected_head(core_db, tmp_path) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    _reset_to_revision(dsn, PREVIOUS_HEAD)
    status, evidence = _invoke(tmp_path, dsn, mode="apply")
    assert status == 0
    assert _heads(dsn) == (ALEMBIC_HEAD,)
    assert evidence["before_revisions"] == [PREVIOUS_HEAD]
    assert evidence["expected_revision"] == ALEMBIC_HEAD
    assert evidence["final_revisions"] == [ALEMBIC_HEAD]
    assert evidence["mutation_attempted"] is True
    assert evidence["mutation_occurred"] is True
    assert evidence["result"] == "applied"


def test_already_current_is_deterministic_noop(core_db, tmp_path) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    status, evidence = _invoke(tmp_path, dsn, mode="apply")
    assert status == 0
    assert evidence["result"] == "already_current"
    assert evidence["before_revisions"] == [ALEMBIC_HEAD]
    assert evidence["final_revisions"] == [ALEMBIC_HEAD]
    assert evidence["mutation_attempted"] is False
    assert evidence["mutation_occurred"] is False


def test_wrong_identity_and_test_production_shape_fail_before_mutation(core_db, tmp_path) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    actual = _database_name(dsn)
    status, evidence = _invoke(
        tmp_path,
        dsn,
        mode="apply",
        expected=f"{actual}_wrong",
        evidence_name="wrong-identity.json",
    )
    assert status == 1
    assert evidence["error"]["rule"] == "database_url_identity_mismatch"
    assert evidence["mutation_attempted"] is False
    assert _heads(dsn) == (ALEMBIC_HEAD,)

    prod_shaped = make_url(dsn).set(database="dish_release_prod").render_as_string(hide_password=False)
    status, evidence = _invoke(
        tmp_path,
        prod_shaped,
        mode="apply",
        expected="dish_release_prod",
        evidence_name="test-prod-shape.json",
    )
    assert status == 1
    assert evidence["error"]["rule"] == "test_database_identity_not_disposable"
    assert evidence["before_revisions"] is None
    assert evidence["mutation_attempted"] is False


def test_production_confirmation_mismatch_fails_before_database_access(core_db, tmp_path) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    prod_dsn = make_url(dsn).set(database="dish_release_prod").render_as_string(hide_password=False)
    status, evidence = _invoke(
        tmp_path,
        prod_dsn,
        mode="apply",
        environment="production",
        expected="dish_release_prod",
        confirmation="dish_release_prod_typo",
        evidence_name="prod-confirmation.json",
    )
    assert status == 1
    assert evidence["error"]["rule"] == "production_confirmation_mismatch"
    assert evidence["before_revisions"] is None
    assert evidence["mutation_attempted"] is False


def test_ahead_unexpected_and_multiple_heads_fail_closed(core_db, tmp_path) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    _rewrite_heads(dsn, "9999_future_or_foreign")
    status, evidence = _invoke(tmp_path, dsn, mode="apply", evidence_name="ahead.json")
    assert status == 1
    assert evidence["error"]["rule"] == "database_revision_ahead_or_unexpected"
    assert evidence["before_revisions"] == ["9999_future_or_foreign"]
    assert evidence["mutation_attempted"] is False

    _rewrite_heads(dsn, PREVIOUS_HEAD, "9999_other_branch")
    status, evidence = _invoke(tmp_path, dsn, mode="apply", evidence_name="multiple.json")
    assert status == 1
    assert evidence["error"]["rule"] == "database_multiple_heads"
    assert evidence["before_revisions"] == [PREVIOUS_HEAD, "9999_other_branch"]
    assert evidence["mutation_attempted"] is False


def test_divergent_known_revision_fault_injection_fails_before_mutation(core_db, tmp_path, monkeypatch) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    divergent = "fault_injected_divergent"
    _rewrite_heads(dsn, divergent)

    class Revision:
        def __init__(self, revision: str):
            self.revision = revision

    class DivergentScript:
        def iterate_revisions(self, _head, _base):
            return [Revision(ALEMBIC_HEAD), Revision(PREVIOUS_HEAD)]

        def walk_revisions(self):
            return [Revision(ALEMBIC_HEAD), Revision(PREVIOUS_HEAD), Revision(divergent)]

    monkeypatch.setattr(migrate, "_repository_script", lambda: DivergentScript())
    status, evidence = _invoke(tmp_path, dsn, mode="apply", evidence_name="divergent.json")
    assert status == 1
    assert evidence["error"]["rule"] == "database_revision_divergent"
    assert evidence["before_revisions"] == [divergent]
    assert evidence["mutation_attempted"] is False


def test_migration_execution_failure_and_post_apply_mismatch_block_promotion(core_db, tmp_path, monkeypatch) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    _reset_to_revision(dsn, PREVIOUS_HEAD)

    def fail_upgrade(*_args, **_kwargs):
        raise RuntimeError("injected migration failure password=must-not-leak")

    monkeypatch.setattr(migrate.command, "upgrade", fail_upgrade)
    status, evidence = _invoke(tmp_path, dsn, mode="apply", evidence_name="execution-failure.json")
    assert status == 1
    assert evidence["error"]["rule"] == "migration_execution_failed"
    assert evidence["final_revisions"] == [PREVIOUS_HEAD]
    assert evidence["mutation_attempted"] is True
    assert evidence["mutation_occurred"] is None
    assert "must-not-leak" not in json.dumps(evidence)
    assert "Do not restart/promote" in evidence["next_action"]

    monkeypatch.setattr(migrate.command, "upgrade", lambda *_args, **_kwargs: None)
    status, evidence = _invoke(tmp_path, dsn, mode="apply", evidence_name="post-mismatch.json")
    assert status == 1
    assert evidence["error"]["rule"] == "post_apply_revision_mismatch"
    assert evidence["final_revisions"] == [PREVIOUS_HEAD]
    assert evidence["mutation_attempted"] is True
    assert evidence["mutation_occurred"] is None


def test_evidence_is_redacted_and_binds_environment_source_and_heads(core_db, tmp_path, capsys) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    _reset_to_revision(dsn, PREVIOUS_HEAD)
    status, evidence = _invoke(tmp_path, dsn, mode="check", evidence_name="redaction.json")
    assert status == 0
    assert evidence["environment"] == "test"
    assert evidence["database_identity"]["expected"] == _database_name(dsn)
    assert evidence["database_identity"]["observed"] == _database_name(dsn)
    assert evidence["source_commit"] == _source_commit()
    assert evidence["before_revisions"] == [PREVIOUS_HEAD]
    assert evidence["expected_revision"] == ALEMBIC_HEAD
    assert evidence["final_revisions"] == [PREVIOUS_HEAD]
    assert evidence["result"] == "pending"
    password = make_url(dsn).password
    combined = (tmp_path / "redaction.json").read_text(encoding="utf-8") + capsys.readouterr().out + capsys.readouterr().err
    if password:
        assert str(password) not in combined
    assert (tmp_path / "redaction.json").stat().st_mode & 0o077 == 0


def test_stale_schema_startup_is_nonretryable_but_database_unavailable_is_retryable(core_db, tmp_path) -> None:
    dsn = postgresql_dsn()
    assert dsn is not None
    expected = _database_name(dsn)
    _reset_to_revision(dsn, PREVIOUS_HEAD)
    assert shadow_worker_main(_shadow_args(dsn, expected, tmp_path)) == 78

    unavailable = make_url(dsn).set(host="127.0.0.1", port=1).render_as_string(hide_password=False)
    assert shadow_worker_main(_shadow_args(unavailable, expected, tmp_path)) == 1
