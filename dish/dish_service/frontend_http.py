"""Private-listener HTTP adapter for the authenticated frontend surface."""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import http
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from dish_pg.frontend_board_query import BoardReadUnavailable
from dish_pg.frontend_detail_query import TaskDetailIneligible
from .frontend_admission import (
    MAX_LOGIN_BODY_BYTES,
    MAX_LOGOUT_BODY_BYTES,
    FrontendRequestError,
    read_json_object,
    resolve_static_candidate,
    reject_ambiguous_security_headers,
    require_request_header_bounds,
    require_contract,
    require_csrf_header,
    require_host,
    require_state_changing_origin,
    session_cookie,
)
from .frontend_auth import FrontendAuthFailure
from .frontend_security import valid_session_token
from .frontend_board import BoardCapacityExceeded, BoardConfigurationInvalid, MAX_SEARCH_QUERY_LENGTH
from .frontend_contract import FRONTEND_CONTRACT_VERSION
from .frontend_detail import DetailCapacityExceeded, TaskNotFound
from .frontend_tokens import CursorInvalid, CursorStale
from .frontend_private_runtime import FrontendDataReadsDisabled

LOG = logging.getLogger("dish.frontend.private")
_TASK_RE = re.compile(r"^/frontend/tasks/((?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SECTION_RE = re.compile(r"^/frontend/sections/(r1s-[A-Za-z0-9_-]{27})/tasks$")
_HTML_TASK_RE = re.compile(r"^/dishes/(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[^/?#]{1,600}$")
_PUBLIC_STATIC_PREFIXES = ("/assets/", "/styles/", "/js/")
_MAX_STATIC_BYTES = 10 * 1024 * 1024

def is_frontend_get(path: str) -> bool:
    return path in {"/", "/admin", "/login", "/frontend/session", "/frontend/board", "/frontend/search", "/frontend/admin", "/openapi/frontend.json"} or path.startswith(_PUBLIC_STATIC_PREFIXES) or path.startswith("/frontend/sections/") or path.startswith("/frontend/tasks/") or path.startswith("/dishes/")

def is_frontend_post(path: str) -> bool:
    return path in {"/frontend/login", "/frontend/logout"}

def dispatch_get(handler, runtime) -> bool:
    parsed = urlsplit(handler.path)
    path = parsed.path
    if not is_frontend_get(path):
        return False
    if handler.server.surface_mode == "action" or runtime is None:
        handler._write_json(http.HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        return True
    try:
        require_request_header_bounds(handler.headers)
        reject_ambiguous_security_headers(handler.headers)
        require_host(handler.headers, origin=runtime.settings.origin)
        if path.startswith(_PUBLIC_STATIC_PREFIXES):
            _serve_static(handler, runtime, path)
        elif path == "/login":
            _serve_html(handler, runtime, login=True)
        elif path in {"/", "/admin"} or _HTML_TASK_RE.fullmatch(path):
            _serve_protected_html(handler, runtime, path, parsed.query)
        elif path == "/frontend/session":
            _session(handler, runtime, parsed.query)
        elif path == "/frontend/board":
            _protected_json(handler, runtime, parsed.query, runtime.board)
        elif path == "/frontend/search":
            _search(handler, runtime, parsed.query)
        elif path == "/frontend/admin":
            _protected_json(handler, runtime, parsed.query, runtime.admin)
        elif path == "/openapi/frontend.json":
            _protected_json(handler, runtime, parsed.query, runtime.openapi_document)
        elif match := _SECTION_RE.fullmatch(path):
            _continuation(handler, runtime, match.group(1), parsed.query)
        elif match := _TASK_RE.fullmatch(path):
            _detail(handler, runtime, match.group(1), parsed.query)
        else:
            _write_api_error(handler, http.HTTPStatus.NOT_FOUND, "request_invalid", "Frontend route is not available.")
    except FrontendRequestError as exc:
        _write_api_error(handler, exc.status, exc.code, exc.message)
    except Exception as exc:
        LOG.error("private frontend GET failed type=%s", type(exc).__name__)
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "internal_error", "Frontend request could not be completed.")
    return True


def dispatch_post(handler, runtime) -> bool:
    path = urlsplit(handler.path).path
    if not is_frontend_post(path):
        return False
    if handler.server.surface_mode == "action" or runtime is None:
        handler._write_json(http.HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
        return True
    try:
        require_request_header_bounds(handler.headers)
        reject_ambiguous_security_headers(handler.headers, logout=path == "/frontend/logout")
        require_host(handler.headers, origin=runtime.settings.origin)
        require_contract(handler.headers)
        require_state_changing_origin(handler.headers, origin=runtime.settings.origin)
        if path == "/frontend/login":
            _login(handler, runtime)
        else:
            _logout(handler, runtime)
    except FrontendRequestError as exc:
        _write_api_error(handler, exc.status, exc.code, exc.message)
    except Exception as exc:
        LOG.error("private frontend POST failed type=%s", type(exc).__name__)
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "internal_error", "Frontend request could not be completed.")
    return True


def _login(handler, runtime) -> None:
    token = session_cookie(handler.headers, ambiguous_code="request_invalid")
    if token is not None and not valid_session_token(token):
        token = None
    payload = read_json_object(handler, max_bytes=MAX_LOGIN_BODY_BYTES)
    if set(payload) != {"password"} or not isinstance(payload["password"], str) or not 1 <= len(payload["password"]) <= 1024:
        _write_api_error(handler, http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Login request is invalid.")
        return
    try:
        result = runtime.auth.login(password=payload["password"], peer=handler.client_address[0], presented_token=token)
    except FrontendAuthFailure as exc:
        _write_auth_error(handler, exc)
        return
    cookie = (
        f"__Host-dish_frontend_session={result.token}; Max-Age=604800; Path=/; "
        "Secure; HttpOnly; SameSite=Strict"
    )
    _write_api_json(handler, http.HTTPStatus.OK, {}, extra_headers={"Set-Cookie": cookie})


def _session(handler, runtime, query: str) -> None:
    if query:
        _write_api_error(handler, http.HTTPStatus.BAD_REQUEST, "request_invalid", "Session request is invalid.")
        return
    require_contract(handler.headers)
    token = _required_session(handler)
    try:
        state = runtime.auth.bootstrap(token)
    except FrontendAuthFailure as exc:
        _write_auth_error(handler, exc)
        return
    _write_api_json(handler, http.HTTPStatus.OK, {
        "expires_at": state.principal.expires_at.isoformat(),
        "remaining_seconds": state.remaining_seconds,
        "csrf_proof": state.csrf_proof,
    })


def _logout(handler, runtime) -> None:
    token = session_cookie(handler.headers, ambiguous_code="logout_unavailable")
    if not token:
        _write_api_error(handler, http.HTTPStatus.UNAUTHORIZED, "auth_required", "Authentication is required.")
        return
    csrf = require_csrf_header(handler.headers)
    payload = read_json_object(handler, max_bytes=MAX_LOGOUT_BODY_BYTES)
    if payload:
        _write_api_error(handler, http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Logout request is invalid.")
        return
    try:
        runtime.auth.logout(token, csrf=csrf)
    except FrontendAuthFailure as exc:
        _write_auth_error(handler, exc)
        return
    # Do not mutate the shared cookie on logout response. A delayed response must
    # never clear a newer replacement login from another tab. The represented
    # server session is already revoked; the next successful login replaces the
    # invalid cookie atomically with its own single Set-Cookie outcome.
    _write_api_json(handler, http.HTTPStatus.OK, {})


def _protected_json(handler, runtime, query: str, operation) -> None:
    require_contract(handler.headers)
    if query:
        _write_api_error(handler, http.HTTPStatus.BAD_REQUEST, "request_invalid", "Frontend request is invalid.")
        return
    token = _required_session(handler)
    if not _validate_before(handler, runtime, token):
        return
    try:
        payload = operation()
    except BoardConfigurationInvalid:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "board_configuration_invalid", "Board configuration is invalid.")
        return
    except BoardCapacityExceeded:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "board_capacity_exceeded", "Board data exceeds configured capacity.")
        return
    except FrontendDataReadsDisabled:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Frontend observation reads are not activated.")
        return
    except BoardReadUnavailable:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Frontend data is temporarily unavailable.")
        return
    except Exception as exc:
        LOG.error("private frontend read failed type=%s", type(exc).__name__)
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "internal_error", "Frontend data could not be loaded.")
        return
    if _validate_before(handler, runtime, token):
        _write_api_json(handler, http.HTTPStatus.OK, payload)


def _search(handler, runtime, query: str) -> None:
    require_contract(handler.headers)
    token = _required_session(handler)
    if not _validate_before(handler, runtime, token):
        return
    values = parse_qsl(query, keep_blank_values=True)
    if (
        len(values) != 1
        or values[0][0] != "q"
        or not 1 <= len(values[0][1].strip()) <= MAX_SEARCH_QUERY_LENGTH
    ):
        _write_api_error(handler, http.HTTPStatus.BAD_REQUEST, "request_invalid", "Search request is invalid.")
        return
    try:
        payload = runtime.search(values[0][1])
    except BoardConfigurationInvalid:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "board_configuration_invalid", "Board configuration is invalid.")
        return
    except FrontendDataReadsDisabled:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Frontend observation reads are not activated.")
        return
    except BoardReadUnavailable:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Search is temporarily unavailable.")
        return
    except Exception as exc:
        LOG.error("private frontend search failed type=%s", type(exc).__name__)
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "internal_error", "Search could not be completed.")
        return
    if _validate_before(handler, runtime, token):
        _write_api_json(handler, http.HTTPStatus.OK, payload)


def _continuation(handler, runtime, section_id: str, query: str) -> None:
    require_contract(handler.headers)
    token = _required_session(handler)
    if not _validate_before(handler, runtime, token): return
    values = parse_qsl(query, keep_blank_values=True)
    if len(values) != 1 or values[0][0] != "cursor" or not values[0][1]:
        _write_api_error(handler, http.HTTPStatus.BAD_REQUEST, "request_invalid", "Continuation request is invalid."); return
    try:
        payload = runtime.continuation(section_route_id=section_id, cursor=values[0][1])
    except CursorInvalid:
        _write_api_error(handler, http.HTTPStatus.BAD_REQUEST, "cursor_invalid", "Continuation cursor is invalid."); return
    except CursorStale:
        _write_api_error(handler, http.HTTPStatus.CONFLICT, "cursor_stale", "Continuation cursor is stale."); return
    except FrontendDataReadsDisabled:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Frontend observation reads are not activated."); return
    except BoardReadUnavailable:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Board data is temporarily unavailable."); return
    if _validate_before(handler, runtime, token): _write_api_json(handler, http.HTTPStatus.OK, payload)


def _detail(handler, runtime, task_id: str, query: str) -> None:
    require_contract(handler.headers)
    token = _required_session(handler)
    if query:
        _write_api_error(handler, http.HTTPStatus.BAD_REQUEST, "request_invalid", "Task request is invalid."); return
    if not _validate_before(handler, runtime, token): return
    try:
        payload = runtime.detail(task_route_id=task_id)
    except TaskNotFound:
        _write_api_error(handler, http.HTTPStatus.NOT_FOUND, "task_not_found", "Task was not found."); return
    except TaskDetailIneligible:
        _write_api_error(handler, http.HTTPStatus.CONFLICT, "task_ineligible", "Task is not eligible for this board."); return
    except DetailCapacityExceeded:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "detail_capacity_exceeded", "Task detail exceeds configured capacity."); return
    except FrontendDataReadsDisabled:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Frontend observation reads are not activated."); return
    except BoardReadUnavailable:
        _write_api_error(handler, http.HTTPStatus.SERVICE_UNAVAILABLE, "service_unavailable", "Task detail is temporarily unavailable."); return
    if _validate_before(handler, runtime, token): _write_api_json(handler, http.HTTPStatus.OK, payload)


def _serve_protected_html(handler, runtime, path: str, query: str) -> None:
    token = session_cookie(handler.headers)
    if not token:
        _redirect_login(handler, path, query); return
    try:
        runtime.auth.validate(token)
    except FrontendAuthFailure:
        _redirect_login(handler, path, query); return
    _serve_html(handler, runtime, login=False)


def _serve_html(handler, runtime, *, login: bool) -> None:
    body = (runtime.static_root / "index.html").read_text(encoding="utf-8")
    body = body.replace('name="dish-runtime-mode" content="local-observation"', f'name="dish-runtime-mode" content="{runtime.browser_runtime_mode}"')
    body = body.replace(
        'name="dish-refresh-interval-seconds" content="25"',
        f'name="dish-refresh-interval-seconds" content="{runtime.settings.refresh_interval_seconds}"',
    )
    _write_bytes(handler, http.HTTPStatus.OK, body.encode("utf-8"), "text/html; charset=utf-8", html=True)

def _serve_static(handler, runtime, path: str) -> None:
    candidate = resolve_static_candidate(runtime.static_root, path)
    if candidate is None:
        _write_bytes(handler, http.HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8"); return
    if not candidate.is_file() or candidate.stat().st_size > _MAX_STATIC_BYTES:
        _write_bytes(handler, http.HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8"); return
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    _write_bytes(handler, http.HTTPStatus.OK, candidate.read_bytes(), content_type)


def _required_session(handler) -> str:
    token = session_cookie(handler.headers)
    if not token:
        raise FrontendRequestError(http.HTTPStatus.UNAUTHORIZED, "auth_required", "Authentication is required.")
    return token


def _validate_before(handler, runtime, token: str) -> bool:
    try: runtime.auth.validate(token)
    except FrontendAuthFailure as exc:
        _write_auth_error(handler, exc); return False
    return True


def _redirect_login(handler, path: str, query: str) -> None:
    del query  # Return targets use the closed board/deep-link path grammar only.
    target = path
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    location = f"/login?return=rt1.{quote(encoded)}"
    _write_bytes(handler, http.HTTPStatus.SEE_OTHER, b"", "text/plain", extra_headers={"Location": location}, html=True)


def _write_auth_error(handler, exc: FrontendAuthFailure) -> None:
    statuses = {
        "auth_required": http.HTTPStatus.UNAUTHORIZED, "session_expired": http.HTTPStatus.UNAUTHORIZED,
        "session_revoked": http.HTTPStatus.UNAUTHORIZED, "session_unavailable": http.HTTPStatus.SERVICE_UNAVAILABLE,
        "logout_unavailable": http.HTTPStatus.SERVICE_UNAVAILABLE, "login_invalid": http.HTTPStatus.UNAUTHORIZED,
        "login_throttled": http.HTTPStatus.TOO_MANY_REQUESTS, "csrf_rejected": http.HTTPStatus.FORBIDDEN,
        "service_unavailable": http.HTTPStatus.SERVICE_UNAVAILABLE,
    }
    _write_api_error(handler, statuses.get(exc.code, http.HTTPStatus.SERVICE_UNAVAILABLE), exc.code, exc.message, retry=exc.retry_after_seconds)


def _write_api_error(handler, status: http.HTTPStatus, code: str, message: str, *, retry: int | None = None) -> None:
    error = {"code": code, "message": message}
    if retry is not None: error["retry_after_seconds"] = retry
    _write_api_json(handler, status, {"error": error})


def _write_api_json(handler, status: http.HTTPStatus, payload, *, extra_headers=None) -> None:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = {"X-Dish-Frontend-Contract": FRONTEND_CONTRACT_VERSION}
    if extra_headers: headers.update(extra_headers)
    _write_bytes(handler, status, body, "application/json; charset=utf-8", extra_headers=headers)


def _write_bytes(handler, status: http.HTTPStatus, body: bytes, content_type: str, *, extra_headers=None, html: bool = False) -> None:
    handler.close_connection = True
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    handler.send_header("Referrer-Policy", "no-referrer")
    if html:
        handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
        handler.send_header("Permissions-Policy", "accelerometer=(), ambient-light-sensor=(), autoplay=(), camera=(), display-capture=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), midi=(), payment=(), publickey-credentials-get=(), screen-wake-lock=(), serial=(), usb=(), xr-spatial-tracking=()")
        handler.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; manifest-src 'none'; worker-src 'none'")
    if extra_headers:
        for name, value in extra_headers.items(): handler.send_header(name, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    if body: handler.wfile.write(body)
