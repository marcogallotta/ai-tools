"""Service entry points delegate lifecycle and route ownership explicitly."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from dish_service.application import DishService
from dish_service.http_routing import resolve_post_route


def test_agent_and_admin_entry_points_delegate_to_request_coordinators():
    for method_name, attribute in (
        ("execute_agent", "_agent_requests"),
        ("execute_admin", "_admin_requests"),
    ):
        source = inspect.getsource(getattr(DishService, method_name))
        tree = ast.parse(source.lstrip())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        assert len(calls) == 1
        assert attribute in source


def test_declarative_post_routes_preserve_surface_and_identifiers():
    cases = {
        "/v1/commands/start": ("agent", "start"),
        "/v1/action/submit": ("action", "submit"),
        "/v1/leases/op-1/renew": ("lease", "renew-lease"),
        "/v1/admin/leases/op-1/recover": ("admin-lease", "recover-lease"),
        "/v1/admin/backups/restore": ("admin-backup", "backup-restore"),
        "/v1/admin/recover": ("admin", "recover"),
    }
    for path, expected in cases.items():
        route = resolve_post_route(path)
        assert route is not None
        assert (route.surface, route.command) == expected
    assert resolve_post_route("/v1/unknown") is None


def test_http_handler_no_longer_owns_route_shape_conditionals():
    source = Path("dish_service/http.py").read_text(encoding="utf-8")
    assert 'parts[:2] == ["v1", "commands"]' not in source
    assert "resolve_post_route(path)" in source
