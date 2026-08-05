"""Submit the exact preplanned first PostgreSQL request and capture its evidence.

This helper performs one HTTP request and never retries.  The input is the same
mode-0600 JSON object recorded by ``dish-pg-release first-admission-plan``.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class FirstAdmissionRequestError(ValueError):
    """The plan, environment, or target response is unsafe or inconsistent."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_atomic(path: Path, value: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_canonical_json_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FirstAdmissionRequestError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _load_plan(path: Path) -> tuple[dict[str, Any], bytes, Path]:
    source = path.expanduser()
    if not source.is_absolute():
        raise FirstAdmissionRequestError("plan path must be absolute")
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FirstAdmissionRequestError("plan must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise FirstAdmissionRequestError("plan must be owner-only (mode 0600 or stricter)")
    source = source.resolve(strict=True)
    raw = source.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs)
    except UnicodeDecodeError as exc:
        raise FirstAdmissionRequestError("plan is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise FirstAdmissionRequestError(f"plan is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise FirstAdmissionRequestError("plan root must be an object")
    return value, raw, source


def _uuid(value: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise FirstAdmissionRequestError(f"{field} must be a UUID") from exc


def _validated_plan(path: Path) -> tuple[dict[str, Any], bytes, Path]:
    plan, raw, source = _load_plan(path)
    if "expected_projection_events" in plan:
        raise FirstAdmissionRequestError(
            "expected_projection_events is derived by release authority and must not be supplied"
        )
    if plan.get("command_name") != "start":
        raise FirstAdmissionRequestError("first admission must use command_name=start")
    if plan.get("principal_class") != "agent":
        raise FirstAdmissionRequestError("first admission HTTP helper requires principal_class=agent")
    request_id = _uuid(plan.get("request_id"), field="request_id")
    run_id = _uuid(plan.get("run_id"), field="run_id")
    task_id = _uuid(plan.get("task_id"), field="task_id")
    arguments = plan.get("command_arguments")
    if not isinstance(arguments, dict):
        raise FirstAdmissionRequestError("command_arguments must be an object")
    if "task_gid" in arguments:
        raise FirstAdmissionRequestError("first admission must use canonical task_id, not task_gid")
    argument_task_id = _uuid(arguments.get("task_id"), field="command_arguments.task_id")
    if argument_task_id != task_id:
        raise FirstAdmissionRequestError("task_id conflicts with command_arguments.task_id")
    owner_id = str(plan.get("owner_id") or "").strip()
    if not owner_id:
        raise FirstAdmissionRequestError("owner_id must be nonblank")
    validated = dict(plan)
    validated.update(
        {
            "request_id": request_id,
            "run_id": run_id,
            "task_id": task_id,
            "owner_id": owner_id,
            "command_arguments": dict(arguments),
        }
    )
    return validated, raw, source


def submit_first_admission(
    *,
    plan_path: Path,
    service_url: str,
    token: str,
    connect_timeout: float,
    response_timeout: float,
) -> dict[str, Any]:
    plan, plan_raw, resolved_plan = _validated_plan(plan_path)
    clean_service_url = service_url.strip().rstrip("/")
    clean_token = token.strip()
    if not clean_service_url:
        raise FirstAdmissionRequestError("service URL environment variable is required")
    if not clean_token:
        raise FirstAdmissionRequestError("service token environment variable is required")
    for label, value in (("connect", connect_timeout), ("response", response_timeout)):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise FirstAdmissionRequestError(f"{label} timeout must be finite and positive")
    parsed = urlsplit(clean_service_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FirstAdmissionRequestError("service URL must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FirstAdmissionRequestError("service URL cannot contain credentials, query, or fragment")
    base_path = parsed.path.rstrip("/")
    request_path = f"{base_path}/v1/commands/start" or "/v1/commands/start"
    request_body = {
        "arguments": plan["command_arguments"],
        "client": {"run_id": plan["run_id"], "request_id": plan["request_id"]},
    }
    request_raw = _canonical_json_bytes(request_body)
    report_base = {
        "format": "dish-first-admission-request-evidence-v1",
        "plan_path": str(resolved_plan),
        "plan_sha256": _sha256_bytes(plan_raw),
        "owner_id": plan["owner_id"],
        "request": {
            "method": "POST",
            "path": request_path,
            "command_name": "start",
            "task_id": plan["task_id"],
            "run_id": plan["run_id"],
            "request_id": plan["request_id"],
            "body_sha256": _sha256_bytes(request_raw),
        },
        "retried": False,
    }
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(
        parsed.hostname,
        parsed.port,
        timeout=float(connect_timeout),
    )
    response_status: int | None = None
    response_raw = b""
    request_started = False
    try:
        connection.connect()
        if connection.sock is None:
            raise FirstAdmissionRequestError("service connection did not expose a socket")
        connection.sock.settimeout(float(response_timeout))
        request_started = True
        connection.request(
            "POST",
            request_path,
            body=request_raw,
            headers={
                "Authorization": f"Bearer {clean_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        response_status = response.status
        response_raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        report = {
            **report_base,
            "status": "fail",
            "ok": False,
            "delivery_state": "unknown" if request_started else "not_sent",
            "transport_error_type": type(exc).__name__,
            "transport_error": "first-admission request transport failed without retry",
            "response": None,
        }
        report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
        return report
    finally:
        connection.close()
    try:
        response_value = json.loads(
            response_raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs
        )
        response_error = None
    except (UnicodeDecodeError, json.JSONDecodeError, FirstAdmissionRequestError) as exc:
        response_value = None
        response_error = f"target returned invalid JSON: {type(exc).__name__}"
    if response_value is not None and not isinstance(response_value, dict):
        response_error = "target response root is not an object"
        response_value = None
    data = response_value.get("data") if isinstance(response_value, dict) else None
    successful = (
        response_status is not None
        and 200 <= response_status < 300
        and isinstance(response_value, dict)
        and response_value.get("ok") is True
        and response_value.get("command") == "start"
        and isinstance(data, dict)
        and data.get("request_id") == plan["request_id"]
    )
    report = {
        **report_base,
        "status": "pass" if successful else "fail",
        "ok": successful,
        "delivery_state": "response_received",
        "response": {
            "http_status": response_status,
            "body_sha256": _sha256_bytes(response_raw),
            "json": response_value,
            "error": response_error,
        },
    }
    report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-first-admission-request")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service-url-env", default="DISH_SERVICE_URL_PROD")
    parser.add_argument("--token-env", default="DISH_SERVICE_TOKEN_PROD")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--response-timeout", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = submit_first_admission(
            plan_path=args.plan,
            service_url=os.environ.get(args.service_url_env, ""),
            token=os.environ.get(args.token_env, ""),
            connect_timeout=args.connect_timeout,
            response_timeout=args.response_timeout,
        )
    except (FirstAdmissionRequestError, OSError, ValueError) as exc:
        report = {
            "format": "dish-first-admission-request-evidence-v1",
            "status": "fail",
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "retried": False,
        }
        report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
    _write_atomic(args.output, report)
    print(
        json.dumps(
            {
                "ok": bool(report.get("ok")),
                "path": str(args.output),
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if report.get("ok") is True else 1
