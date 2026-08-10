"""Loopback-only HTTP serving for the Stage 3 local PostgreSQL board."""
from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

from dish_pg.frontend_board_query import BoardReadUnavailable
from dish_pg.frontend_detail_query import TaskDetailIneligible
from dish_service.frontend_board import BoardCapacityExceeded, BoardConfigurationInvalid
from dish_service.frontend_contract import FRONTEND_CONTRACT_VERSION
from dish_service.frontend_detail import DetailCapacityExceeded, TaskNotFound
from dish_service.frontend_tokens import CursorInvalid, CursorStale

LOG = logging.getLogger("dish.frontend.local")
_MAX_STATIC_BYTES = 10 * 1024 * 1024
_SECTION_ROUTE_RE = re.compile(r"r1s-[A-Za-z0-9_-]{27}")
_TASK_ROUTE_RE = re.compile(r"(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class LocalBoardBackend(Protocol):
    def bootstrap(self) -> dict[str, Any]: ...

    def continuation(self, *, section_route_id: str, cursor: str) -> dict[str, Any]: ...

    def detail(self, *, task_route_id: str) -> dict[str, Any]: ...


class FrontendLocalServer(ThreadingHTTPServer):
    """Static + read-only board server that may bind only to loopback."""

    daemon_threads = False
    block_on_close = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        backend: LocalBoardBackend,
        static_root: Path,
    ) -> None:
        if not is_loopback_host(address[0]):
            raise ValueError("local frontend server may bind only to loopback")
        root = static_root.resolve()
        if not root.is_dir() or not (root / "index.html").is_file():
            raise ValueError(f"frontend build directory is incomplete: {root}")
        self.backend = backend
        self.static_root = root
        super().__init__(address, FrontendLocalRequestHandler)
        bound_host, bound_port = self.server_address[:2]
        self.allowed_host_authorities = frozenset(
            {_loopback_authority(str(bound_host), int(bound_port))}
        )


class FrontendLocalRequestHandler(BaseHTTPRequestHandler):
    server: FrontendLocalServer
    protocol_version = "HTTP/1.1"

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        LOG.info(
            "local_frontend_http remote=%s method=%s status=%s bytes=%s",
            self.client_address[0],
            getattr(self, "command", "-"),
            code,
            size,
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        # BaseHTTPRequestHandler normally includes the raw request target here.
        # Frontend route identities and cursors are deliberately omitted from logs.
        LOG.info(
            "local_frontend_http remote=%s method=%s event=http_message",
            self.client_address[0],
            getattr(self, "command", "-"),
        )

    def do_GET(self) -> None:  # noqa: N802
        if not self._require_local_host():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/frontend/board":
            self._board(parsed.query)
            return
        task_route = _task_route_from_path(parsed.path)
        if task_route is not None:
            self._task_detail(task_route, parsed.query)
            return
        section_route = _section_route_from_path(parsed.path)
        if section_route is not None:
            self._section_page(section_route, parsed.query)
            return
        if parsed.path.startswith("/frontend/"):
            self._write_api_error(
                HTTPStatus.NOT_FOUND,
                "request_invalid",
                "Frontend route is not available.",
            )
            return
        self._static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._require_local_host():
            return
        self._reject_mutation()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._require_local_host():
            return
        self._reject_mutation()

    def do_PATCH(self) -> None:  # noqa: N802
        if not self._require_local_host():
            return
        self._reject_mutation()

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._require_local_host():
            return
        self._reject_mutation()

    def _require_local_host(self) -> bool:
        values = self.headers.get_all("Host", [])
        if len(values) == 1 and values[0] in self.server.allowed_host_authorities:
            return True
        self._write_text(HTTPStatus.BAD_REQUEST, b"Bad request\n")
        return False

    def _board(self, query: str) -> None:
        if not self._require_contract():
            return
        if query:
            self._write_api_error(
                HTTPStatus.BAD_REQUEST,
                "request_invalid",
                "Board request is invalid.",
            )
            return
        try:
            self._write_api_json(HTTPStatus.OK, self.server.backend.bootstrap())
        except BoardCapacityExceeded:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "board_capacity_exceeded",
                "Board capacity is exceeded.",
            )
        except BoardConfigurationInvalid:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "board_configuration_invalid",
                "Board configuration is invalid.",
            )
        except BoardReadUnavailable:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_unavailable",
                "Board data is temporarily unavailable.",
            )
        except Exception as exc:  # closed local observation surface
            LOG.error("local frontend board read failed type=%s", type(exc).__name__)
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "internal_error",
                "Board data could not be loaded.",
            )

    def _section_page(self, section_route_id: str, query: str) -> None:
        if not self._require_contract():
            return
        if _SECTION_ROUTE_RE.fullmatch(section_route_id) is None:
            self._write_api_error(
                HTTPStatus.BAD_REQUEST,
                "request_invalid",
                "Section request is invalid.",
            )
            return
        values = parse_qsl(query, keep_blank_values=True)
        if len(values) != 1 or values[0][0] != "cursor" or not values[0][1]:
            self._write_api_error(
                HTTPStatus.BAD_REQUEST,
                "request_invalid",
                "Continuation request is invalid.",
            )
            return
        cursor = values[0][1]
        try:
            payload = self.server.backend.continuation(
                section_route_id=section_route_id,
                cursor=cursor,
            )
            self._write_api_json(HTTPStatus.OK, payload)
        except CursorInvalid:
            self._write_api_error(
                HTTPStatus.BAD_REQUEST,
                "cursor_invalid",
                "Continuation cursor is invalid.",
            )
        except CursorStale:
            self._write_api_error(
                HTTPStatus.CONFLICT,
                "cursor_stale",
                "Continuation cursor is stale.",
            )
        except BoardCapacityExceeded:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "board_capacity_exceeded",
                "Board capacity is exceeded.",
            )
        except BoardConfigurationInvalid:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "board_configuration_invalid",
                "Board configuration is invalid.",
            )
        except BoardReadUnavailable:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_unavailable",
                "Board data is temporarily unavailable.",
            )
        except Exception as exc:  # closed local observation surface
            LOG.error("local frontend continuation read failed type=%s", type(exc).__name__)
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "internal_error",
                "Board data could not be loaded.",
            )


    def _task_detail(self, task_route_id: str, query: str) -> None:
        if not self._require_contract():
            return
        if query or _TASK_ROUTE_RE.fullmatch(task_route_id) is None:
            self._write_api_error(HTTPStatus.BAD_REQUEST, "request_invalid", "Task request is invalid.")
            return
        try:
            self._write_api_json(HTTPStatus.OK, self.server.backend.detail(task_route_id=task_route_id))
        except TaskNotFound:
            self._write_api_error(HTTPStatus.NOT_FOUND, "task_not_found", "Task was not found.")
        except TaskDetailIneligible:
            self._write_api_error(HTTPStatus.CONFLICT, "task_ineligible", "Task is not eligible for this board.")
        except DetailCapacityExceeded:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "detail_capacity_exceeded",
                "Task detail exceeds the configured local capacity.",
            )
        except BoardReadUnavailable:
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_unavailable",
                "Task detail is temporarily unavailable.",
            )
        except Exception as exc:
            LOG.error("local frontend detail read failed type=%s", type(exc).__name__)
            self._write_api_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "internal_error",
                "Task detail could not be loaded.",
            )

    def _require_contract(self) -> bool:
        values = self.headers.get_all("X-Dish-Frontend-Contract", [])
        if values == [FRONTEND_CONTRACT_VERSION]:
            return True
        # contract_mismatch is intentionally client-local. Unsupported request
        # versions use the registered server outcome and advertise this build's
        # one supported contract version on the response.
        self._write_api_error(
            HTTPStatus.FORBIDDEN,
            "client_update_required",
            "Frontend client update is required.",
        )
        return False

    def _reject_mutation(self) -> None:
        if urlsplit(self.path).path.startswith("/frontend/"):
            self._write_api_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "request_invalid",
                "This local frontend is read-only.",
                extra_headers={"Allow": "GET"},
            )
            return
        self._write_text(
            HTTPStatus.METHOD_NOT_ALLOWED,
            b"Method not allowed\n",
            extra_headers={"Allow": "GET"},
        )

    def _static(self, request_path: str) -> None:
        decoded = unquote(request_path)
        requested = "/index.html" if decoded == "/" else decoded
        candidate = (self.server.static_root / requested.lstrip("/")).resolve()
        if not _is_within(candidate, self.server.static_root):
            self._write_text(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        if not candidate.is_file() and not Path(requested).suffix:
            candidate = self.server.static_root / "index.html"
        try:
            if not candidate.is_file() or candidate.stat().st_size > _MAX_STATIC_BYTES:
                raise FileNotFoundError
            body = candidate.read_bytes()
        except (OSError, FileNotFoundError):
            self._write_text(HTTPStatus.NOT_FOUND, b"Not found\n")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
        }:
            content_type = f"{content_type}; charset=utf-8"
        self._write_bytes(HTTPStatus.OK, body, content_type=content_type)

    def _write_api_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._write_api_json(
            status,
            {"error": {"code": code, "message": message}},
            extra_headers=extra_headers,
        )

    def _write_api_json(
        self,
        status: HTTPStatus,
        payload: Any,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"X-Dish-Frontend-Contract": FRONTEND_CONTRACT_VERSION}
        if extra_headers:
            headers.update(extra_headers)
        self._write_bytes(
            status,
            body,
            content_type="application/json; charset=utf-8",
            extra_headers=headers,
        )

    def _write_text(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._write_bytes(
            status,
            body,
            content_type="text/plain; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _write_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-src 'none'; worker-src 'none'; media-src 'none'; manifest-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def _task_route_from_path(path: str) -> str | None:
    prefix = "/frontend/tasks/"
    if not path.startswith(prefix):
        return None
    encoded = path[len(prefix):]
    if not encoded or "/" in encoded:
        return None
    return unquote(encoded)

def _section_route_from_path(path: str) -> str | None:
    prefix = "/frontend/sections/"
    suffix = "/tasks"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded = path[len(prefix) : -len(suffix)]
    if not encoded or "/" in encoded:
        return None
    return unquote(encoded)


def is_loopback_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    addresses = {item[4][0].split("%", 1)[0] for item in infos}
    return bool(addresses) and all(ipaddress.ip_address(value).is_loopback for value in addresses)


def _loopback_authority(host: str, port: int) -> str:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError("local frontend bound host must resolve to a literal loopback address") from exc
    if not address.is_loopback:
        raise ValueError("local frontend bound host must be loopback")
    rendered = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"{rendered}:{port}"


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False
