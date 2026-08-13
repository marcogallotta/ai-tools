from __future__ import annotations

from types import SimpleNamespace

import pytest

from dish_pg import migrate
from dish_pg.migrate import RoutineMigrationError, _validate_current_revision, _validate_target


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
