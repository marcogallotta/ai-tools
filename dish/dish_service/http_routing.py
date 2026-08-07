"""Declarative POST route matching for the Dish HTTP boundary."""

from __future__ import annotations

from dataclasses import dataclass

from dish_tool.admin_command_spec import ADMIN_COMMAND_SPECS

from .command_spec import ACTION_LEASE_COMMAND


def _admin_command(name: str) -> str:
    return ADMIN_COMMAND_SPECS[name].name


@dataclass(frozen=True)
class PostRoute:
    surface: str
    command: str
    parts: tuple[str, ...]
    parameters: dict[str, str]


@dataclass(frozen=True)
class _RoutePattern:
    pattern: tuple[str, ...]
    surface: str
    fixed_command: str | None = None
    command_parameter: str | None = None

    def match(self, parts: tuple[str, ...]) -> PostRoute | None:
        if len(parts) != len(self.pattern):
            return None
        parameters: dict[str, str] = {}
        for expected, actual in zip(self.pattern, parts, strict=True):
            if expected.startswith("{") and expected.endswith("}"):
                parameters[expected[1:-1]] = actual
            elif expected != actual:
                return None
        command = self.fixed_command
        if command is None and self.command_parameter is not None:
            command = parameters[self.command_parameter]
        if command is None:
            raise RuntimeError("POST route has no command source")
        return PostRoute(self.surface, command, parts, parameters)


_POST_ROUTES = (
    _RoutePattern(("v1", "commands", "{command}"), "agent", command_parameter="command"),
    _RoutePattern(("v1", "action", "{command}"), "action", command_parameter="command"),
    _RoutePattern(("v1", "leases", "{operation_id}", "renew"), "lease", ACTION_LEASE_COMMAND),
    _RoutePattern(
        ("v1", "admin", "leases", "{operation_id}", "recover"),
        "admin-lease",
        _admin_command("recover-lease"),
    ),
    _RoutePattern(
        ("v1", "admin", "leases", "expire"),
        "admin-lease-expiry",
        _admin_command("expire-lease"),
    ),
    _RoutePattern(
        ("v1", "admin", "backups", "create"),
        "admin-backup",
        _admin_command("backup-create"),
    ),
    _RoutePattern(
        ("v1", "admin", "backups", "restore"),
        "admin-backup",
        _admin_command("backup-restore"),
    ),
    _RoutePattern(("v1", "admin", "argument-failures", "{command}"), "admin-argument-failure", command_parameter="command"),
    _RoutePattern(("v1", "argument-failures", "{command}"), "argument-failure", command_parameter="command"),
    _RoutePattern(("v1", "admin", "{command}"), "admin", command_parameter="command"),
)


def resolve_post_route(path: str) -> PostRoute | None:
    parts = tuple(part for part in path.split("/") if part)
    for route in _POST_ROUTES:
        matched = route.match(parts)
        if matched is not None:
            return matched
    return None
