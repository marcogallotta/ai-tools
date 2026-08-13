from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from dish_pg import migrate
from dish_pg.migrate import RoutineMigrationError, _validate_current_revision, _validate_target


def _git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def test_repository_head_inconsistency_fails_before_target_use(monkeypatch) -> None:
    class FakeScript:
        def get_heads(self):
            return ["other_head"]

    monkeypatch.setattr(migrate.ScriptDirectory, "from_config", lambda _cfg: FakeScript())
    with pytest.raises(RoutineMigrationError, match="repository migration heads") as caught:
        migrate._repository_script()
    assert caught.value.rule == "repository_head_inconsistency"


def test_environment_identity_rules_fail_closed() -> None:
    with pytest.raises(RoutineMigrationError) as caught:
        _validate_target(
            environment="test",
            database_url="postgresql+psycopg://u:p@localhost/dish_prod",
            expected_database_name="dish_prod",
            apply=False,
            confirmation=None,
        )
    assert caught.value.rule == "test_database_identity_not_disposable"

    with pytest.raises(RoutineMigrationError) as caught:
        _validate_target(
            environment="production",
            database_url="postgresql+psycopg://u:p@localhost/dish_prod",
            expected_database_name="dish_prod",
            apply=True,
            confirmation="wrong",
        )
    assert caught.value.rule == "production_confirmation_mismatch"


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_dirty_checkout_fails_before_source_identity_or_database_access(
    tmp_path, monkeypatch, dirty_kind
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Dish Test")
    _git(repo, "config", "user.email", "dish-test@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")

    if dirty_kind == "tracked":
        tracked.write_text("dirty\n", encoding="utf-8")
    else:
        (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    monkeypatch.setattr(migrate, "DISH_ROOT", repo)
    monkeypatch.setattr(
        migrate,
        "_read_database_state",
        lambda _database_url: pytest.fail("dirty checkout must fail before database access"),
    )
    args = SimpleNamespace(
        environment="test",
        database_url="postgresql+psycopg://u:p@localhost/dish_dirty_test",
        expected_database_name="dish_dirty_test",
        source_commit=head,
        apply=False,
        check=True,
        confirm_database_name=None,
    )
    journal = SimpleNamespace(write=lambda _payload: None)

    evidence, status = migrate.run(args, journal)

    assert status == 1
    assert evidence["source_commit"] is None
    assert evidence["before_revisions"] is None
    assert evidence["mutation_attempted"] is False
    assert evidence["error"]["rule"] == "source_checkout_dirty"


def test_known_nonancestor_revision_is_divergent() -> None:
    class Revision:
        def __init__(self, revision):
            self.revision = revision

    script = SimpleNamespace(
        iterate_revisions=lambda _head, _base: [
            Revision(migrate.ALEMBIC_HEAD),
            Revision("ancestor"),
        ],
        walk_revisions=lambda: [
            Revision(migrate.ALEMBIC_HEAD),
            Revision("ancestor"),
            Revision("divergent"),
        ],
    )
    with pytest.raises(RoutineMigrationError) as caught:
        _validate_current_revision(script, ("divergent",))
    assert caught.value.rule == "database_revision_divergent"
