"""Ambiguity-intolerant admission helpers for private frontend requests."""
from __future__ import annotations

import json
from dataclasses import dataclass
import http
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .frontend_contract import FRONTEND_CONTRACT_VERSION

SESSION_COOKIE = "__Host-dish_frontend_session"
MAX_LOGIN_BODY_BYTES = 16384
MAX_LOGOUT_BODY_BYTES = 1024
MAX_COOKIE_BYTES = 4096
MAX_SESSION_VALUE_BYTES = 256
MAX_HEADER_COUNT = 64
MAX_HEADER_BYTES = 32768


@dataclass(frozen=True, slots=True)
class FrontendRequestError(Exception):
    status: http.HTTPStatus
    code: str
    message: str


def resolve_static_candidate(root: Path, path: str) -> Path | None:
    decoded = unquote(path).lstrip("/")
    prefix, separator, relative = decoded.partition("/")
    if prefix not in {"assets", "styles", "js", "fixtures"} or not separator or not relative:
        return None
    base = (root / prefix).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def require_request_header_bounds(headers) -> None:
    raw_items = list(headers.raw_items())
    if len(raw_items) > MAX_HEADER_COUNT:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request headers exceed the allowed count.")
    size = sum(len(name) + len(value) + 4 for name, value in raw_items)
    if size > MAX_HEADER_BYTES:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request headers exceed the allowed size.")


def reject_ambiguous_security_headers(headers, *, logout: bool = False) -> None:
    mappings = {
        "Host": (http.HTTPStatus.FORBIDDEN, "origin_rejected"),
        "Origin": (http.HTTPStatus.FORBIDDEN, "origin_rejected"),
        "X-Dish-Frontend-Contract": (http.HTTPStatus.FORBIDDEN, "client_update_required"),
        "X-Dish-CSRF": (
            (http.HTTPStatus.FORBIDDEN, "csrf_rejected")
            if logout else (http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid")
        ),
    }
    for name, (status, code) in mappings.items():
        if len(headers.get_all(name, [])) > 1:
            raise FrontendRequestError(status, code, "Request headers are ambiguous.")


def singleton(headers, name: str, *, required: bool = False) -> str | None:
    values = headers.get_all(name, [])
    if len(values) > 1:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request headers are ambiguous.")
    if not values:
        if required:
            raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "A required request header is missing.")
        return None
    return values[0]


def require_contract(headers) -> None:
    values = headers.get_all("X-Dish-Frontend-Contract", [])
    if len(values) != 1 or values[0] != FRONTEND_CONTRACT_VERSION:
        raise FrontendRequestError(http.HTTPStatus.FORBIDDEN, "client_update_required", "The frontend must be reloaded.")


def require_host(headers, *, origin: str) -> None:
    expected = urlsplit(origin).netloc
    values = headers.get_all("Host", [])
    if len(values) != 1 or values[0].lower() != expected.lower():
        raise FrontendRequestError(http.HTTPStatus.FORBIDDEN, "origin_rejected", "The request origin was rejected.")


def require_state_changing_origin(headers, *, origin: str) -> None:
    values = headers.get_all("Origin", [])
    if len(values) != 1 or values[0] != origin:
        raise FrontendRequestError(http.HTTPStatus.FORBIDDEN, "origin_rejected", "The request origin was rejected.")
    expected = {
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    for name, value in expected.items():
        values = headers.get_all(name, [])
        if len(values) != 1 or values[0].lower() != value:
            raise FrontendRequestError(http.HTTPStatus.FORBIDDEN, "origin_rejected", "The request origin was rejected.")


def session_cookie(headers, *, ambiguous_code: str = "auth_required") -> str | None:
    raw_headers = headers.get_all("Cookie", [])
    if sum(len(item) for item in raw_headers) > MAX_COOKIE_BYTES:
        raise FrontendRequestError(http.HTTPStatus.UNAUTHORIZED, ambiguous_code, "Authentication is required.")
    found: list[str] = []
    for header in raw_headers:
        for part in header.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                found.append(value)
    if len(found) > 1:
        status = http.HTTPStatus.SERVICE_UNAVAILABLE if ambiguous_code == "logout_unavailable" else http.HTTPStatus.UNAUTHORIZED
        raise FrontendRequestError(status, ambiguous_code, "The frontend session is ambiguous.")
    if not found:
        return None
    if len(found[0]) > MAX_SESSION_VALUE_BYTES:
        return found[0][: MAX_SESSION_VALUE_BYTES + 1]
    return found[0]


def require_csrf_header(headers) -> str:
    values = headers.get_all("X-Dish-CSRF", [])
    if len(values) != 1 or not 22 <= len(values[0]) <= 256 or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in values[0]):
        raise FrontendRequestError(http.HTTPStatus.FORBIDDEN, "csrf_rejected", "Logout verification was rejected.")
    return values[0]


def read_json_object(handler, *, max_bytes: int) -> dict[str, Any]:
    content_types = handler.headers.get_all("Content-Type", [])
    if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
        raise FrontendRequestError(http.HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "media_type_unsupported", "Content-Type application/json is required.")
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Content-Length is required.")
    try:
        length = int(lengths[0])
    except ValueError as exc:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Content-Length is invalid.") from exc
    if length < 0 or length > max_bytes:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request body is outside the allowed size.")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request body is incomplete.")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise FrontendRequestError(http.HTTPStatus.UNPROCESSABLE_ENTITY, "request_invalid", "Request body must be a JSON object.")
    return payload
