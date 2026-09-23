from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.systemd_units import (
    assert_directives,
    assert_user_manager_compatible,
    load_unit,
    parse_unit,
)

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"

COMMON_HARDENING = {
    "NoNewPrivileges": ("true",),
    "UMask": ("0077",),
    "ProtectKernelTunables": ("true",),
    "ProtectControlGroups": ("true",),
    "RestrictSUIDSGID": ("true",),
    "LockPersonality": ("true",),
    "RestrictAddressFamilies": ("AF_UNIX AF_INET AF_INET6",),
    "PrivateTmp": ("true",),
    "ProtectSystem": ("strict",),
    "ProtectHome": ("read-only",),
}
HARDENING_DIRECTIVES = (
    "NoNewPrivileges",
    "UMask",
    "ProtectKernelTunables",
    "ProtectControlGroups",
    "RestrictSUIDSGID",
    "LockPersonality",
    "RestrictAddressFamilies",
    "PrivateTmp",
    "ProtectSystem",
    "ProtectHome",
    "ReadWritePaths",
)
DEPENDENCY_DIRECTIVES = ("After", "Wants", "Requires", "BindsTo", "Conflicts", "PartOf")
NETWORK = {
    "After": ("network-online.target",),
    "Wants": ("network-online.target",),
}
DEFAULT_INSTALL = {"WantedBy": ("default.target",)}


@dataclass(frozen=True)
class UnitContract:
    directives: Mapping[str, Mapping[str, tuple[str, ...]]]


def _service(
    exec_start: str,
    *,
    environment_files: tuple[str, ...] = (),
    restart: tuple[str, ...] = ("on-failure",),
    restart_prevent: tuple[str, ...] = (),
    unit: Mapping[str, tuple[str, ...]] = NETWORK,
    service_extra: Mapping[str, tuple[str, ...]] | None = None,
    install: Mapping[str, tuple[str, ...]] | None = DEFAULT_INSTALL,
    hardening: Mapping[str, tuple[str, ...]] = COMMON_HARDENING,
) -> UnitContract:
    service = {
        "ExecStart": (exec_start,),
        "EnvironmentFile": environment_files,
        "Restart": restart,
        "RestartPreventExitStatus": restart_prevent,
        **{
            directive: hardening.get(directive, ())
            for directive in HARDENING_DIRECTIVES
        },
        **(service_extra or {}),
    }
    directives: dict[str, Mapping[str, tuple[str, ...]]] = {
        "Unit": {
            directive: unit.get(directive, ()) for directive in DEPENDENCY_DIRECTIVES
        },
        "Service": service,
        "Install": {"WantedBy": () if install is None else install["WantedBy"]},
    }
    return UnitContract(directives)


SHADOW_COMMAND = (
    "/home/marco/ai-tools/dish/.venv/bin/python -m dish_pg.shadow_worker_entrypoint "
    "--database-url ${DISH_PG_DATABASE_URL} "
    "--expected-database-name ${DISH_PG_EXPECTED_DATABASE_NAME} "
    "--spool-path ${DISH_DARK_LAUNCH_SPOOL_PATH} "
    "--baseline-id ${DISH_DARK_LAUNCH_BASELINE_ID} "
    "--worker-id ${DISH_DARK_LAUNCH_WORKER_ID} "
    "--cursor-secret-file ${DISH_PG_CURSOR_SECRET_FILE} "
    "--comparator-release ${DISH_DARK_LAUNCH_COMPARATOR_RELEASE} "
    "--kill-switch ${DISH_DARK_LAUNCH_KILL_SWITCH} "
    "--busy-timeout-ms ${DISH_DARK_LAUNCH_BUSY_TIMEOUT_MS} "
    "--max-spool-bytes ${DISH_DARK_LAUNCH_MAX_SPOOL_BYTES} "
    "--max-spool-records ${DISH_DARK_LAUNCH_MAX_SPOOL_RECORDS} "
    "--min-free-bytes ${DISH_DARK_LAUNCH_MIN_FREE_BYTES} "
    "--reservation-ttl-seconds ${DISH_DARK_LAUNCH_RESERVATION_TTL_SECONDS} "
    "--delivered-retention-seconds ${DISH_DARK_LAUNCH_DELIVERED_RETENTION_SECONDS}"
)


UNIT_CONTRACTS = {
    "dish-action-router.service": _service(
        "/usr/bin/caddy run --resume --config /home/marco/ai-tools/dish/deploy/caddy/dish-action-router.json",
        unit={
            "After": (
                "network-online.target dish-service-test.service dish-service-prod.service",
            ),
            "Wants": ("network-online.target",),
        },
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish/caddy",),
        },
    ),
    "dish-frontend-caddy.service": _service(
        "/usr/bin/caddy run --config /home/marco/ai-tools/dish/deploy/caddy/dish-frontend-local.Caddyfile --adapter caddyfile",
        unit={
            "After": ("network-online.target dish-frontend-private.service",),
            "Wants": ("network-online.target",),
            "Requires": ("dish-frontend-private.service",),
            "PartOf": ("dish-frontend.target",),
        },
        install={"WantedBy": ("dish-frontend.target",)},
        service_extra={
            "ReadWritePaths": (
                "/home/marco/.config/caddy /home/marco/.local/share/caddy",
            ),
        },
    ),
    "dish-frontend-private.service": _service(
        "/home/marco/ai-tools/dish/dish-service",
        environment_files=("/home/marco/.config/dish-service/frontend-local.env",),
        unit={**NETWORK, "PartOf": ("dish-frontend.target",)},
        service_extra={
            "ReadWritePaths": (
                "/home/marco/.local/state/dish/frontend-local-auth /home/marco/ai-tools/dish/frontend",
            ),
        },
        install=None,
    ),
    "dish-frontend-test-caddy.service": _service(
        "/usr/bin/caddy run --config /home/marco/ai-tools/dish/deploy/caddy/dish-frontend-test.Caddyfile --adapter caddyfile",
        environment_files=("/home/marco/.config/dish-service/frontend-test-caddy.env",),
        unit={
            "After": ("network-online.target dish-service-test.service",),
            "Wants": ("network-online.target",),
            "Requires": ("dish-service-test.service",),
            "BindsTo": ("dish-service-test.service",),
        },
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish/test",),
        },
    ),
    "dish-mcp-tunnel.service": _service(
        "/home/marco/.local/bin/tunnel-client run --profile dish-mcp",
        environment_files=("/home/marco/.config/dish-service/mcp-tunnel.env",),
        unit={
            "After": (
                "network-online.target dish-service-prod.service dish-mcp.service",
            ),
            "Wants": ("network-online.target",),
            "Requires": ("dish-mcp.service",),
        },
    ),
    "dish-mcp.service": _service(
        "/home/marco/ai-tools/dish/.venv/bin/python -m dish_service.mcp_server",
        environment_files=("/home/marco/.config/dish-service/mcp.env",),
        hardening={
            "NoNewPrivileges": ("true",),
            "UMask": ("0077",),
            "PrivateTmp": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("read-only",),
            "ReadWritePaths": (
                "/home/marco/.local/share/fastmcp /home/marco/honest-pantry",
            ),
        },
    ),
    "dish-postgres-backup.service": _service(
        "/home/marco/ai-tools/dish/.venv/bin/python scripts/dish-pg-scheduled-backup run",
        environment_files=("/home/marco/.config/dish-service/postgres-backup.env",),
        restart=(),
        unit={
            "After": ("network-online.target dish-postgres-prod.service",),
            "Wants": ("network-online.target",),
        },
        service_extra={
            "StateDirectory": ("dish/prod/postgresql-backups",),
            "StateDirectoryMode": ("0700",),
            "ProtectSystem": ("full",),
        },
        install=None,
    ),
    "dish-postgres-prod.service": _service(
        "/usr/bin/docker compose -p dish-postgres-prod -f deploy/postgresql/compose.yaml up -d",
        environment_files=("/home/marco/.config/dish-service/postgres-prod.env",),
        restart=(),
        hardening={},
    ),
    "dish-postgres-test.service": _service(
        "/usr/bin/docker compose -p postgresql -f deploy/postgresql/compose.yaml up -d",
        environment_files=("/home/marco/.config/dish-service/postgres-test.env",),
        restart=(),
        hardening={},
    ),
    "dish-service-prod.service": _service(
        "/home/marco/.local/share/dish/prod-current/dish/dish-service",
        environment_files=(
            "/home/marco/.config/dish-service/prod.env",
            "/home/marco/.config/dish-service/postgres-prod.env",
        ),
        restart_prevent=("78",),
        service_extra={
            "WorkingDirectory": ("/home/marco/.local/share/dish/prod-current/dish",),
            "Environment": (
                "DISH_RELEASE_MANIFEST=/home/marco/.local/share/dish/prod-current/.dish-release.json",
            ),
            "ReadWritePaths": ("/home/marco/.local/state/dish/prod",),
        },
    ),
    "dish-service-test-legacy.service": _service(
        "/home/marco/ai-tools/dish/dish-service",
        environment_files=("/home/marco/.config/dish-service/test-legacy.env",),
        restart_prevent=("78",),
        unit={**NETWORK, "Conflicts": ("dish-service.service",)},
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish/test-legacy",),
        },
    ),
    "dish-service-test.service": _service(
        "/home/marco/ai-tools/dish/dish-service",
        environment_files=("/home/marco/.config/dish-service/test.env",),
        restart_prevent=("78",),
        unit={
            "After": ("network-online.target dish-postgres-test.service",),
            "Wants": ("network-online.target dish-postgres-test.service",),
            "Conflicts": ("dish-service.service dish-shadow-worker-test.service",),
        },
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish/test",),
        },
    ),
    "dish-service.service": _service(
        "/home/marco/ai-tools/dish/dish-service",
        environment_files=("/home/marco/.config/dish-service/service.env",),
        restart_prevent=("78",),
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish",),
        },
    ),
    "dish-shadow-worker-test.service": _service(
        SHADOW_COMMAND,
        environment_files=("/home/marco/.config/dish-service/dark-launch-test.env",),
        restart_prevent=("78",),
        unit={
            "After": (
                "network-online.target dish-service-test.service dish-postgres-test.service",
            ),
            "Wants": ("network-online.target",),
        },
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish/test",),
        },
    ),
    "dish-shadow-worker.service": _service(
        SHADOW_COMMAND,
        environment_files=("/home/marco/.config/dish-service/dark-launch.env",),
        restart_prevent=("78",),
        unit={
            "After": ("network-online.target dish-service-prod.service",),
            "Wants": ("network-online.target",),
        },
        service_extra={
            "ReadWritePaths": ("/home/marco/.local/state/dish/prod",),
        },
    ),
    "dish-postgres-backup.timer": UnitContract(
        {
            "Timer": {
                "OnCalendar": ("*-*-* *:00:00 UTC",),
                "AccuracySec": ("1min",),
                "Persistent": ("true",),
                "Unit": ("dish-postgres-backup.service",),
            },
            "Install": {"WantedBy": ("timers.target",)},
        }
    ),
    "dish-frontend.target": UnitContract(
        {
            "Unit": {
                "Requires": (
                    "dish-frontend-private.service dish-frontend-caddy.service",
                ),
                "After": ("dish-frontend-private.service dish-frontend-caddy.service",),
            },
            "Install": DEFAULT_INSTALL,
        }
    ),
}


def _assert_contract(name: str, text: str) -> None:
    unit = parse_unit(text)
    assert_directives(unit, UNIT_CONTRACTS[name].directives)
    assert_user_manager_compatible(unit)


def test_contract_table_is_complete_for_every_shipped_dish_user_unit() -> None:
    shipped = {
        path.name
        for pattern in ("dish-*.service", "dish-*.timer", "dish-*.target")
        for path in SYSTEMD.glob(pattern)
    }
    assert set(UNIT_CONTRACTS) == shipped


@pytest.mark.parametrize("name", sorted(UNIT_CONTRACTS))
def test_shipped_dish_user_unit_contract(name: str) -> None:
    unit = load_unit(SYSTEMD / name)
    assert_directives(unit, UNIT_CONTRACTS[name].directives)
    assert_user_manager_compatible(unit)


@pytest.mark.parametrize(
    ("name", "before", "after"),
    [
        ("dish-mcp.service", "-m dish_service.mcp_server", "-m wrong.module"),
        ("dish-service.service", "service.env", "wrong.env"),
        ("dish-service.service", "Restart=on-failure", "Restart=always"),
        ("dish-service.service", "ProtectSystem=strict", "ProtectSystem=full"),
        (
            "dish-mcp-tunnel.service",
            "Requires=dish-mcp.service",
            "Requires=dish-service.service",
        ),
        (
            "dish-service-prod.service",
            "/home/marco/.local/share/dish/prod-current/dish/dish-service",
            "/home/marco/ai-tools/dish/dish-service",
        ),
    ],
)
def test_contract_rejects_mutated_behavior_critical_directive(
    name: str, before: str, after: str
) -> None:
    original = (SYSTEMD / name).read_text(encoding="utf-8")
    assert before in original
    with pytest.raises(AssertionError):
        _assert_contract(name, original.replace(before, after, 1))


def test_contract_rejects_system_manager_only_directive() -> None:
    original = (SYSTEMD / "dish-service.service").read_text(encoding="utf-8")
    mutated = original.replace("[Service]\n", "[Service]\nUser=marco\n", 1)
    with pytest.raises(AssertionError, match="system-manager-only"):
        _assert_contract("dish-service.service", mutated)


def test_parser_preserves_repeated_directives_and_continuations() -> None:
    unit = parse_unit(
        """[Service]
EnvironmentFile=/one
EnvironmentFile=/two
ExecStart=/bin/example --first \\
  --second
"""
    )
    assert unit.values("Service", "EnvironmentFile") == ("/one", "/two")
    assert unit.values("Service", "ExecStart") == ("/bin/example --first --second",)
