"""SSRF-safe bounded outbound HTTPS fetch for connected-agent file transport.

Dish fetches exactly one caller-declared file per call. The resolved IP is
pinned before the TLS handshake so a caller cannot use DNS rebinding to
redirect the connection after the safety check runs.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from dish_tool.errors import DishRuleError

MAX_FETCH_BYTES = 10_000_000
CONNECT_TIMEOUT_SECONDS = 10.0
TOTAL_FETCH_TIMEOUT_SECONDS = 30.0
_CHUNK_BYTES = 65536


@dataclass(frozen=True)
class FetchedFile:
    sha256: str
    byte_count: int


def _fetch_error(message: str, rule: str) -> DishRuleError:
    return DishRuleError("BACKEND_REJECTED", message, rule=rule, retryable=True)


def _reject_unsafe_address(address: str) -> None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "download_link host resolved to an invalid address",
            rule="file_transport_address_invalid",
        ) from exc
    unsafe = (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )
    if unsafe:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "download_link host is not a safe public address",
            rule="file_transport_address_forbidden",
        )


def _resolve_safe_address(hostname: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _fetch_error(
            "download_link host could not be resolved", "file_transport_resolve_failed"
        ) from exc
    if not infos:
        raise _fetch_error(
            "download_link host resolved to no address", "file_transport_resolve_failed"
        )
    address = infos[0][4][0]
    _reject_unsafe_address(address)
    return address


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPSConnection that connects to a pre-validated IP but keeps the
    original hostname for TLS SNI/certificate verification."""

    def __init__(self, pinned_address: str, hostname: str, port: int, *, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(hostname, port, timeout=timeout, context=context)
        self._pinned_address = pinned_address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_expected_file(download_link: str, *, expected_bytes: object) -> FetchedFile:
    """Fetch download_link over HTTPS with SSRF/size/time bounds.

    expected_bytes bounds the read in addition to the fixed Gate A ceiling; it
    is caller-declared and not itself trusted for anything beyond that bound.
    """
    parts = urlsplit(download_link)
    if parts.scheme != "https" or not parts.hostname:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "download_link must be an https URL",
            rule="file_transport_scheme_invalid",
        )
    hostname = parts.hostname
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    address = _resolve_safe_address(hostname, port)

    try:
        declared_ceiling = int(expected_bytes)
    except (TypeError, ValueError):
        declared_ceiling = MAX_FETCH_BYTES
    read_ceiling = min(MAX_FETCH_BYTES, max(declared_ceiling, 0)) if declared_ceiling > 0 else MAX_FETCH_BYTES

    context = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(
        address, hostname, port, timeout=CONNECT_TIMEOUT_SECONDS, context=context
    )
    deadline = time.monotonic() + TOTAL_FETCH_TIMEOUT_SECONDS
    try:
        try:
            conn.request("GET", path, headers={"Connection": "close"})
            response = conn.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise _fetch_error(
                "download_link fetch failed", "file_transport_fetch_failed"
            ) from exc
        if response.status != 200:
            raise _fetch_error(
                f"download_link responded with status {response.status}",
                "file_transport_status_invalid",
            )
        hasher = hashlib.sha256()
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise _fetch_error(
                    "download_link fetch timed out", "file_transport_fetch_timeout"
                )
            remaining = deadline - time.monotonic()
            conn.sock.settimeout(max(0.1, remaining))
            try:
                chunk = response.read(_CHUNK_BYTES)
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise _fetch_error(
                    "download_link fetch failed", "file_transport_fetch_failed"
                ) from exc
            if not chunk:
                break
            total += len(chunk)
            if total > read_ceiling:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "download_link body exceeds the expected/allowed byte ceiling",
                    rule="file_transport_size_exceeded",
                )
            hasher.update(chunk)
        return FetchedFile(sha256=hasher.hexdigest(), byte_count=total)
    finally:
        conn.close()
