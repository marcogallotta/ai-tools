"""Install isolated persistent native PostgreSQL and real user-unit release fixtures."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

from . import prod_release as release
from .release_hot import TEST_CONFIG, Budget, Target, atomic_json, snapshot


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def command(argv, **kwargs):
    return subprocess.run(
        list(map(str, argv)),
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        **kwargs,
    ).stdout.strip()


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def fixture_repository(root, repository, candidate):
    destination = root / "upstream.git"
    command(["git", "clone", "--bare", repository, destination])
    tree = command(["git", "-C", destination, "rev-parse", candidate + "^{tree}"])
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dish TEST fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Dish TEST fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-11-02T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-11-02T00:00:00Z",
    }
    prior = command(
        [
            "git",
            "-C",
            destination,
            "commit-tree",
            tree,
            "-m",
            "Deterministic retained TEST release; identical interface, distinct process identity",
        ],
        env=env,
    )
    command(["git", "-C", destination, "update-ref", "refs/heads/main", candidate])
    return destination, prior


def create_database(root, port, repository):
    binaries = Path("/usr/lib/postgresql/17/bin")
    pgdata = root / "postgres"
    command(
        [
            binaries / "initdb",
            "-D",
            pgdata,
            "--username=dish",
            "--auth=trust",
            "--no-locale",
            "--encoding=UTF8",
        ]
    )
    sockets = root / "sockets"
    sockets.mkdir()
    with (pgdata / "postgresql.conf").open("a") as stream:
        stream.write(
            f"\nport={port}\nlisten_addresses='127.0.0.1'\nunix_socket_directories='{sockets}'\n"
        )
    command(
        [binaries / "pg_ctl", "-D", pgdata, "-l", root / "postgres.log", "-w", "start"]
    )
    command(
        [
            binaries / "createdb",
            "-h",
            "127.0.0.1",
            "-p",
            port,
            "-U",
            "dish",
            "dish_release_test",
        ]
    )
    dsn = f"postgresql+psycopg://dish@127.0.0.1:{port}/dish_release_test"
    from alembic import command as alembic
    from alembic.config import Config

    config = Config(str(repository / "dish/alembic.ini"))
    config.set_main_option(
        "script_location", str(repository / "dish/dish_pg/migrations")
    )
    config.set_main_option("sqlalchemy.url", dsn)
    alembic.upgrade(config, "head")
    from dish_pg.schema_identity import ALEMBIC_HEAD
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from tests.support.postgresql.core import _bootstrap_registry, _uuid_stream

    engine = create_engine(dsn)
    with Session(engine) as session, session.begin():
        context = _bootstrap_registry(
            session,
            _uuid_stream(),
            generation_status="active",
            schema_head=ALEMBIC_HEAD,
        )
    engine.dispose()
    return dsn, str(context["generation_id"]), ALEMBIC_HEAD


def install_target(
    root, repository, upstream, candidate, prior, *, name, qualification=False
):
    root.mkdir(parents=True, mode=0o700)
    dbport, backend_port, mcp_port = free_port(), free_port(), free_port()
    dsn, generation, schema = create_database(root, dbport, repository)
    for directory in ("authority", "oauth", "honest", "operations", "state"):
        (root / directory).mkdir(mode=0o700)
    # Immutable Pantry snapshot for archive filtering and context. No human checkout writes.
    pantry = Path.home() / "honest-pantry"
    command(["git", "clone", "--bare", pantry, root / "honest/upstream.git"])
    command(["git", "clone", pantry, root / "honest/checkout"])
    key = secrets.token_hex(32)
    write_private(root / "jwt-key", key)
    resource = f"http://127.0.0.1:{mcp_port}/mcp"
    from joserfc import jwk, jwt

    token = jwt.encode(
        {"alg": "HS256"},
        {
            "iss": "dish-release-rehearsal",
            "aud": resource,
            "sub": "release-test",
            "client_id": "release-test",
            "exp": int(time.time()) + 86400,
        },
        jwk.OctKey.import_key(key),
    )
    write_private(root / "token", token)
    env = {
        "DISH_PROFILE": "test",
        "DISH_RELEASE_TEST_ROOT": str(root),
        "DISH_PG_DATABASE_URL": dsn,
        "DISH_PG_EXPECTED_DATABASE_NAME": "dish_release_test",
        "DISH_PG_EXPECTED_SCHEMA_HEAD": schema,
        "DISH_PG_EXPECTED_RELEASE": "dish-42619b9",
        "DISH_PG_EXPECTED_GENERATION_ID": generation,
        "DISH_PG_AUTHORITY_STATE_DIR": str(root / "authority"),
        "DISH_PG_CURSOR_SECRET": secrets.token_hex(32),
        "DISH_SERVICE_PORT": str(backend_port),
        "DISH_MCP_BIND_PORT": str(mcp_port),
        "DISH_MCP_RESOURCE_URL": resource,
        "DISH_SERVICE_AGENT_TOKEN": secrets.token_hex(24),
        "DISH_SERVICE_ADMIN_TOKEN": secrets.token_hex(24),
        "DISH_SERVICE_ACTION_TOKEN": secrets.token_hex(24),
        "DISH_DB_PATH": str(root / "state/unused.sqlite3"),
        "DISH_STATE_DIR": str(root / "state"),
        "DISH_HONEST_PATH": str(root / "honest/checkout"),
        "FASTMCP_HOME": str(root / "oauth"),
        "XDG_DATA_HOME": str(root / "oauth/data"),
        "XDG_CONFIG_HOME": str(root / "oauth/config"),
        "DISH_MCP_HONEST_STORE": str(root / "honest/repository.git"),
        "DISH_MCP_HONEST_REMOTE": str(root / "honest/upstream.git"),
        "DISH_MCP_PLANNING_CONTEXT_DB": str(root / "state/planning.sqlite3"),
    }
    write_private(root / "service.env", "".join(f"{k}={v}\n" for k, v in env.items()))
    values = {
        "release_root": str(root / "releases"),
        "current": str(root / "current"),
        "previous": str(root / "previous"),
        "state_root": str(root / "operations"),
        "env_files": [str(root / "service.env")],
        "mcp_env_file": str(root / "service.env"),
        "backend_unit": f"dish-release-test-{name}-backend.service",
        "mcp_unit": f"dish-release-test-{name}-mcp.service",
        "health_url": f"http://127.0.0.1:{backend_port}/health",
        "mcp_url": resource,
        "mcp_local_url": resource,
        "token_file": str(root / "token"),
        "python": str(release.DEFAULT_PYTHON),
        "repository": str(upstream),
        "upstream": str(upstream),
        "oauth_root": str(root / "oauth"),
        "honest_store": str(root / "honest/repository.git"),
        "isolation_root": str(root),
        "database_url": dsn,
        "timeout": 15,
        "recovery_timeout": 15,
        "status_timeout": 3,
    }
    old = release.stage_release(
        upstream, prior, root / "releases", release.DEFAULT_PYTHON
    )
    new = release.stage_release(
        upstream, candidate, root / "releases", release.DEFAULT_PYTHON
    )
    release._set_pointer(root / "current", str(old.root))
    release._set_pointer(root / "previous", str(new.root))
    release._set_pointer(root / "mcp-current", str(old.root))
    units = Path.home() / ".config/systemd/user"
    for component in ("backend", "mcp"):
        pointer = root / (
            "mcp-current" if qualification and component == "mcp" else "current"
        )
        unit = units / values[f"{component}_unit"]
        if unit.exists():
            raise RuntimeError(f"refusing to replace existing unit: {unit}")
        unit.write_text(
            f"[Unit]\nDescription=Isolated Dish release TEST {name} {component}\n[Service]\nType=simple\nWorkingDirectory={pointer}/dish\nEnvironmentFile={root}/service.env\nEnvironment=DISH_RELEASE_MANIFEST={pointer}/.dish-release.json\nExecStart={pointer}/dish/.venv/bin/python -m dish_tool.release_rehearsal_runtime {component}\nKillMode=control-group\nTimeoutStopSec=3\nUMask=0077\n"
        )
    atomic_json(root / "target.json", values)
    return Target("test", values)


def setup(repository, candidate, root):
    if root.exists():
        raise RuntimeError(
            "rehearsal root already exists; preserving all evidence and fixtures"
        )
    root.mkdir(parents=True, mode=0o700)
    upstream, prior = fixture_repository(root, repository, candidate)
    operator = install_target(
        root / "operator", repository, upstream, candidate, prior, name="operator"
    )
    qualification = install_target(
        root / "qualification",
        repository,
        upstream,
        candidate,
        prior,
        name="qualification",
        qualification=True,
    )
    from .release_qualification import QUALIFICATION_CONFIG

    for destination, target in (
        (TEST_CONFIG, operator),
        (QUALIFICATION_CONFIG, qualification),
    ):
        if destination.exists():
            raise RuntimeError(
                f"refusing to replace existing installed TEST configuration {destination}"
            )
        atomic_json(destination, target.values)
    command(["systemctl", "--user", "daemon-reload"])
    for target in (operator, qualification):
        for component in ("backend", "mcp"):
            command(
                ["systemctl", "--user", "start", target.values[f"{component}_unit"]]
            )
    budget = Budget(30)
    while True:
        observed = snapshot(operator, budget)
        if observed["ok"]:
            break
        budget.left()
        time.sleep(0.1)
    atomic_json(
        root / "setup-probe.json",
        {
            "ok": True,
            "candidate": candidate,
            "prior": prior,
            "operator": operator.values,
            "qualification": qualification.values,
            "observed": observed,
            "limitations": [
                "TEST uses separate real JWT bearer trust domain; production GitHub OAuth not commissioned",
                "local fixed upstream does not prove live GitHub fetch",
                "synthetic retained fixture has identical transport source and distinct immutable SHA",
            ],
        },
    )
    return root / "setup-probe.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/state/dish/release-rehearsal"
    )
    args = parser.parse_args()
    print(setup(args.repository.resolve(), args.candidate, args.root.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
