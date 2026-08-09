from __future__ import annotations

from pathlib import Path

import pytest
import uuid
from datetime import timedelta
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from dish_tool.frontend_password_admin import (
    FrontendPasswordAdminSettings,
    provision,
    rotate_password,
)
from dish_pg.frontend_security_models import FrontendSecurityAudit, FrontendSecurityState, FrontendSession
from dish_service.frontend_security import FrontendSecurityConfigurationError, create_restore_fence

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[1]


def _env(db_path: Path, fence_path: Path) -> dict[str, str]:
    return {
        "DISH_FRONTEND_DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
        "DISH_FRONTEND_RESTORE_FENCE_PATH": str(fence_path),
        "DISH_FRONTEND_ARGON2_TIME_COST": "1",
        "DISH_FRONTEND_ARGON2_MEMORY_KIB": "1024",
        "DISH_FRONTEND_ARGON2_PARALLELISM": "1",
        "DISH_FRONTEND_ARGON2_HASH_LEN": "16",
        "DISH_FRONTEND_ARGON2_SALT_LEN": "16",
        "DISH_FRONTEND_ARGON2_MIN_TIME_COST": "1",
        "DISH_FRONTEND_ARGON2_MAX_TIME_COST": "2",
        "DISH_FRONTEND_ARGON2_MIN_MEMORY_KIB": "1024",
        "DISH_FRONTEND_ARGON2_MAX_MEMORY_KIB": "2048",
        "DISH_FRONTEND_ARGON2_MIN_PARALLELISM": "1",
        "DISH_FRONTEND_ARGON2_MAX_PARALLELISM": "2",
        "DISH_SERVICE_AGENT_TOKEN": "agent-secret-not-password",
    }


def _migrate(db_path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path}")
    command.upgrade(config, "head")


def test_password_admin_provisions_and_rotation_invalidates_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "frontend-security.sqlite3"
    fence_path = tmp_path / "frontend-security.fence"
    _migrate(db_path)
    create_restore_fence(fence_path)
    settings = FrontendPasswordAdminSettings.from_mapping(_env(db_path, fence_path))

    provision(settings, "correct horse battery staple")

    engine = create_engine(settings.database_url, future=True)
    try:
        with Session(engine) as session:
            state = session.get(FrontendSecurityState, 1)
            assert state is not None
            assert state.security_generation == 1
            assert state.password_verifier.startswith("$argon2id$")
            session.add(
                FrontendSession(
                    session_id=uuid.uuid4(),
                    token_verifier=b"t" * 32,
                    security_generation=1,
                    restore_fence_sha256=state.restore_fence_sha256,
                    peer_digest=b"p" * 32,
                    issued_at=state.updated_at,
                    expires_at=state.updated_at + timedelta(days=7),
                    revoked_at=None,
                )
            )
            session.commit()

        assert rotate_password(settings, "another correct battery staple") == 2

        with Session(engine) as session:
            state = session.get(FrontendSecurityState, 1)
            assert state is not None
            assert state.security_generation == 2
            assert state.password_verifier.startswith("$argon2id$")
            stored_session = session.scalar(select(FrontendSession))
            assert stored_session is not None and stored_session.revoked_at is not None
            events = list(session.scalars(select(FrontendSecurityAudit.event_type)))
            assert events == ["password_provisioned", "password_rotated", "global_invalidation"]
    finally:
        engine.dispose()


def test_password_admin_rejects_configured_secret_equality(tmp_path: Path) -> None:
    db_path = tmp_path / "frontend-security.sqlite3"
    fence_path = tmp_path / "frontend-security.fence"
    _migrate(db_path)
    create_restore_fence(fence_path)
    settings = FrontendPasswordAdminSettings.from_mapping(_env(db_path, fence_path))

    with pytest.raises(FrontendSecurityConfigurationError, match="configured.*secret"):
        provision(settings, "agent-secret-not-password")
