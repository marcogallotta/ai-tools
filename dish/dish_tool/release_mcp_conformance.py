"""Read-only MCP conformance evidence for production release activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

DEFAULT_MCP_URL = "https://laptop.tail46f0b9.ts.net/dish/mcp"
DEFAULT_TOKEN_FILE = Path("/home/marco/.config/dish-service/mcp-conformance-token")
DEFAULT_RECEIPT_ROOT = Path("/home/marco/.local/state/dish/release-receipts")
DEFAULT_MCP_UNIT = "dish-mcp.service"
_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')
_MAX_RESPONSE_BYTES = 1024 * 1024


class _ReleaseIdentity(Protocol):
    source_commit: str
    schema_head: str


class MCPReleaseConformance:
    """Probe the deployed MCP transport without claiming exact MCP code binding."""

    def __init__(
        self,
        *,
        resource_url: str,
        token_file: Path,
        receipt_root: Path,
        unit: str,
        timeout: float,
        error_type: type[Exception],
        http_request: Callable[..., tuple[int, Mapping[str, str], bytes]] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        parsed = urlparse(resource_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.path.endswith("/mcp")
        ):
            raise error_type("MCP resource URL must be an https URL ending in /mcp")
        self.resource_url = resource_url
        self.token_file = token_file
        self.receipt_root = receipt_root
        self.unit = unit
        self.timeout = timeout
        self.error_type = error_type
        self.http_request = http_request
        self.command_runner = command_runner
        self._preflight: dict[str, Any] | None = None

    def _request(
        self,
        request: urllib.request.Request,
        *,
        expected_status: int = 200,
    ) -> tuple[int, Mapping[str, str], bytes]:
        if self.http_request is not None:
            return self.http_request(request, expected_status=expected_status)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                headers = dict(response.headers.items())
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = dict(exc.headers.items())
            body = exc.read(_MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise self.error_type("MCP conformance request failed") from exc
        if status != expected_status:
            raise self.error_type(
                f"MCP conformance expected HTTP {expected_status}, received {status}"
            )
        if len(body) > _MAX_RESPONSE_BYTES:
            raise self.error_type("MCP conformance response exceeds the bounded limit")
        return status, headers, body

    def preflight(self, release: _ReleaseIdentity) -> None:
        """Prove public discovery before either release pointer is changed."""
        del release
        request = urllib.request.Request(
            self.resource_url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        status, headers, _body = self._request(request, expected_status=401)
        challenge = next(
            (
                value
                for name, value in headers.items()
                if name.lower() == "www-authenticate"
            ),
            "",
        )
        match = _RESOURCE_METADATA.search(challenge)
        if match is None:
            raise self.error_type("MCP authentication challenge omits resource metadata")
        metadata_url = match.group(1)
        parsed_resource = urlparse(self.resource_url)
        parsed_metadata = urlparse(metadata_url)
        if (
            parsed_metadata.scheme != "https"
            or parsed_metadata.netloc != parsed_resource.netloc
            or parsed_metadata.username is not None
            or parsed_metadata.password is not None
        ):
            raise self.error_type("MCP resource metadata URL is not same-origin https")
        metadata_status, _headers, metadata_body = self._request(
            urllib.request.Request(metadata_url, method="GET")
        )
        try:
            metadata = json.loads(metadata_body)
        except (TypeError, ValueError) as exc:
            raise self.error_type("MCP resource metadata is not valid JSON") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("resource") != self.resource_url
        ):
            raise self.error_type("MCP resource metadata identifies a different resource")
        self._preflight = {
            "auth_challenge_status": status,
            "resource_metadata_status": metadata_status,
            "resource_metadata_url": metadata_url,
        }

    def _token(self) -> str:
        try:
            mode = self.token_file.stat().st_mode & 0o777
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise self.error_type("MCP conformance token file is unavailable") from exc
        if mode & 0o077:
            raise self.error_type(
                "MCP conformance token file must not be group/world accessible"
            )
        if not token or "\n" in token or "\r" in token:
            raise self.error_type("MCP conformance token file is invalid")
        return token

    def _rpc(
        self,
        token: str,
        payload: Mapping[str, Any],
        *,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.resource_url,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
        )
        _status, _headers, body = self._request(
            request, expected_status=expected_status
        )
        if expected_status == 202:
            return {}
        try:
            response = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise self.error_type("MCP conformance response is not valid JSON") from exc
        if not isinstance(response, dict) or response.get("error") is not None:
            raise self.error_type("MCP conformance JSON-RPC request failed")
        return response

    def _unit_evidence(self) -> dict[str, Any]:
        try:
            status = self.command_runner(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    self.unit,
                    "--property=ActiveState,SubState,MainPID",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise self.error_type("timed out collecting MCP user-unit status") from exc
        if status.returncode != 0:
            raise self.error_type("cannot collect MCP user-unit status")
        values: dict[str, str] = {}
        for line in status.stdout.splitlines():
            name, separator, value = line.partition("=")
            if separator and name in {"ActiveState", "SubState", "MainPID"}:
                values[name] = value
        if values.get("ActiveState") != "active":
            raise self.error_type("MCP user unit is not active")
        try:
            journal = self.command_runner(
                [
                    "/usr/bin/journalctl",
                    "--user",
                    "-u",
                    self.unit,
                    "--lines=40",
                    "--no-pager",
                    "--output=short-iso",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise self.error_type(
                "timed out collecting bounded MCP user-unit journal evidence"
            ) from exc
        if journal.returncode != 0:
            raise self.error_type("cannot collect bounded MCP user-unit journal evidence")
        encoded = journal.stdout.encode("utf-8", "replace")
        return {
            "manager": "user",
            "unit": self.unit,
            "status": values,
            "journal": {
                "line_limit": 40,
                "line_count": len(journal.stdout.splitlines()),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            },
        }

    def verify_and_write_receipt(self, release: _ReleaseIdentity) -> Path:
        """Run authenticated read-only probes and atomically persist redacted evidence."""
        if self._preflight is None:
            raise self.error_type("MCP pre-activation conformance was not completed")
        token = self._token()
        initialize = self._rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "dish-release-conformance", "version": "1"},
                },
            },
        )
        if not isinstance(initialize.get("result"), dict):
            raise self.error_type("MCP initialize did not return a result")
        self._rpc(
            token,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            expected_status=202,
        )
        read = self._rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "dish_sections",
                    "arguments": {
                        "client": {"run_id": str(uuid.uuid4())},
                        "arguments": {"agent": "gpt"},
                    },
                },
            },
        )
        result = read.get("result")
        if not isinstance(result, dict) or result.get("isError") is True:
            raise self.error_type("MCP read-only conformance call failed")
        receipt = {
            "schema": "dish-release-mcp-conformance-v1",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "backend_release": {
                "binding": "exact_immutable_release",
                "source_commit": release.source_commit,
                "schema_head": release.schema_head,
            },
            "mcp_executable": {
                "binding": "unbound_mutable_checkout",
                "exact_release": False,
            },
            "resource_url": self.resource_url,
            "pre_activation": self._preflight,
            "post_activation": {
                "authenticated_initialize": True,
                "safe_read": "dish_sections",
                "safe_read_ok": True,
            },
            "unit_evidence": self._unit_evidence(),
            "redaction": {
                "token_recorded": False,
                "response_bodies_recorded": False,
                "journal_content_recorded": False,
            },
        }
        self.receipt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.receipt_root, 0o700)
        destination = self.receipt_root / f"{release.source_commit}.json"
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(receipt, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination


__all__ = [
    "DEFAULT_MCP_UNIT",
    "DEFAULT_MCP_URL",
    "DEFAULT_RECEIPT_ROOT",
    "DEFAULT_TOKEN_FILE",
    "MCPReleaseConformance",
]
