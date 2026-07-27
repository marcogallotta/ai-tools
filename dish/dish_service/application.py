"""Transport-neutral shared-service boundary around the existing applications."""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from dish_tool.admin import DishAdminApplication
from dish_tool.backend import AsanaBackend
from dish_tool.commands import DishApplication
from dish_tool.database import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.releases import resolve_release
from dish_tool.results import error_envelope

from .config import ServiceConfig


class DishService:
    """Create a fresh database/application boundary for each request.

    SQLite remains the single shared persistent authority. Opening a connection per
    request avoids sharing sqlite connection objects across HTTP worker threads.
    """

    def __init__(
        self,
        config: ServiceConfig,
        *,
        backend_factory: Callable[[], Any] | None = None,
        release_loader: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.backend_factory = backend_factory or AsanaBackend
        self.release_loader = release_loader

    def _release(self, role: str | None = None, *, include_migrations: bool = False):
        if self.release_loader is not None:
            try:
                return self.release_loader(role, include_migrations=include_migrations)
            except TypeError:
                try:
                    return self.release_loader(role)
                except TypeError:
                    return self.release_loader()
        return resolve_release(
            self.config.honest_root,
            protocol_role=role,
            include_migrations=include_migrations,
        )

    @contextlib.contextmanager
    def _candidate_file(self, arguments: Mapping[str, Any]):
        prepared = dict(arguments)
        text = prepared.pop("file_text", None)
        if text is None:
            yield prepared
            return
        if "file_path" in prepared:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "provide file_text or file_path, not both",
                rule="candidate_transport_conflict",
            )
        if not isinstance(text, str):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "file_text must be a string",
                rule="candidate_text_invalid",
            )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
            handle.write(text)
            path = Path(handle.name)
        prepared["file_path"] = str(path)
        try:
            yield prepared
        finally:
            path.unlink(missing_ok=True)

    def execute_agent(self, command: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        conn = initialize_database(self.config.db_path)
        try:
            app = DishApplication(
                conn,
                self.backend_factory(),
                release_loader=lambda role=None: self._release(role),
            )
            with self._candidate_file(arguments) as prepared:
                return app.execute(command, **prepared)
        except DishRuleError as exc:
            return error_envelope(command, exc)
        finally:
            conn.close()

    def execute_admin(self, command: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        conn = initialize_database(self.config.db_path)
        try:
            app = DishAdminApplication(
                conn,
                backend=self.backend_factory(),
                release_loader=lambda: self._release(None, include_migrations=True),
            )
            with self._candidate_file(arguments) as prepared:
                return app.execute(command, **prepared)
        except DishRuleError as exc:
            return error_envelope(command, exc)
        finally:
            conn.close()

    def health(self) -> dict[str, Any]:
        conn = initialize_database(self.config.db_path)
        try:
            release = self._release(None)
            return {
                "ok": True,
                "service": "dish",
                "database": {"ok": True, "path": str(self.config.db_path)},
                "compatibility": {
                    "ok": True,
                    "protocol_version": release.protocol_version,
                    "schema_version": release.schema_version,
                },
            }
        except DishRuleError as exc:
            return {
                "ok": False,
                "service": "dish",
                "database": {"ok": False, "path": str(self.config.db_path)},
                "compatibility": {"ok": False, "message": str(exc), "rule": exc.rule},
            }
        finally:
            conn.close()
