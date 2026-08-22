#!/usr/bin/env python3
"""Purpose-built read-only MCP tools for the dedicated Integrator model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Mapping

from pr_lifecycle_integrator import IntegratorAudit


VERSION_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_TEXT = 4_000
MAX_LIST = 50


def _clip(value: Any, limit: int = 2_000) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…[truncated]"
    if isinstance(value, Mapping):
        return {str(key): _bounded(child) for key, child in value.items()}
    if isinstance(value, list):
        result = [_bounded(child) for child in value[:MAX_LIST]]
        if len(value) > MAX_LIST:
            result.append({"truncated_items": len(value) - MAX_LIST})
        return result
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _rpc_tool(name: str, description: str, properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "title": name.replace("_", " ").title(),
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": dict(properties),
            "required": required,
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "additionalProperties": True},
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    }


VERSION_PROPERTY = {
    "actionable_version": {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
        "description": "Exact Lifecycle V4 actionable_version from the wake packet.",
    }
}

TOOLS = (
    _rpc_tool(
        "get_integrator_case",
        "Read the exact V4 receipt case and canonical CI consumer decision for one wake version.",
        VERSION_PROPERTY,
        ["actionable_version"],
    ),
    _rpc_tool(
        "get_exact_pr_evidence",
        "Read current GitHub PR/head/check/review evidence only for the PR bound to one wake version.",
        VERSION_PROPERTY,
        ["actionable_version"],
    ),
    _rpc_tool(
        "get_exact_check_log",
        "Read a bounded failure-focused log excerpt only after proving the check belongs to the exact wake head.",
        {
            **VERSION_PROPERTY,
            "check_run_id": {"type": "integer", "minimum": 1},
        },
        ["actionable_version", "check_run_id"],
    ),
    _rpc_tool(
        "get_repair_owner",
        "Read the exact Asana repair task returned by canonical CI ownership for one wake version.",
        VERSION_PROPERTY,
        ["actionable_version"],
    ),
    _rpc_tool(
        "get_prior_integrator_decisions",
        "Read bounded prior Integrator audit records for one wake version.",
        VERSION_PROPERTY,
        ["actionable_version"],
    ),
    _rpc_tool(
        "get_nightly_health",
        "Read recent runs of the existing Full regression workflow and current main; never schedule or rerun it.",
        {},
        [],
    ),
)


class IntegratorReadTools:
    def __init__(self, *, state_dir: Path, repository: str):
        self.state_dir = state_dir.expanduser().resolve()
        self.repository = repository
        if repository != "marcogallotta/ai-tools":
            raise ValueError("Integrator MCP repository is not allowlisted")
        self.state_path = self.state_dir / "state.json"
        self.report_path = self.state_dir / "integrator-report.json"
        self.audit = IntegratorAudit(
            self.state_dir / "integrator-audit.ndjson",
            report_path=self.report_path,
        )

    @staticmethod
    def _version(arguments: Mapping[str, Any]) -> str:
        version = str(arguments.get("actionable_version") or "")
        if VERSION_RE.fullmatch(version) is None:
            raise ValueError("actionable_version must be the exact 64-character V4 identity")
        return version

    def _audit_records(self) -> list[dict[str, Any]]:
        return self.audit.records()

    def _decision(self, version: str) -> dict[str, Any] | None:
        try:
            reports = [_read_json(self.report_path)]
        except FileNotFoundError:
            reports = []
        for record in reversed(self._audit_records()):
            if record.get("event") == "projection_consumed":
                reports.append({"decisions": record.get("decisions") or []})
        for report in reports:
            for value in report.get("decisions") or []:
                if isinstance(value, Mapping) and value.get("actionable_version") == version:
                    return dict(value)
        return None

    def _case(self, version: str) -> dict[str, Any]:
        state = _read_json(self.state_path)
        for receipt in (state.get("receipts") or {}).values():
            if not isinstance(receipt, Mapping):
                continue
            packet = receipt.get("packet") if isinstance(receipt.get("packet"), Mapping) else {}
            versions = [str(value) for value in packet.get("actionable_versions") or []]
            cases = [value for value in packet.get("cases") or [] if isinstance(value, Mapping)]
            for index, candidate in enumerate(versions):
                if candidate != version:
                    continue
                case = dict(cases[index]) if index < len(cases) else {}
                return {
                    "actionable_version": version,
                    "wake_id": receipt.get("wake_id"),
                    "receipt_status": receipt.get("status"),
                    "turn_id": receipt.get("turn_id"),
                    "case": case,
                    "canonical_consumer_decision": self._decision(version),
                }
        raise ValueError("actionable_version is not present in the local V4 receipt ledger")

    @staticmethod
    def _command_json(argv: tuple[str, ...]) -> Any:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=25, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(_bounded(detail))
        return json.loads(result.stdout)

    @staticmethod
    def _command_text(argv: tuple[str, ...]) -> str:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=25, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(_bounded(detail))
        return result.stdout

    def get_integrator_case(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self._case(self._version(arguments))

    def get_exact_pr_evidence(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        version = self._version(arguments)
        resolved = self._case(version)
        case = resolved["case"]
        repository = str(case.get("repository") or "")
        if repository != self.repository:
            raise ValueError("wake packet repository is not allowlisted")
        try:
            pr_number = int(case.get("pr"))
        except (TypeError, ValueError) as exc:
            raise ValueError("wake packet has no exact PR number") from exc
        head = str(case.get("head") or case.get("reviewed_head") or "")
        if re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise ValueError("wake packet has no exact PR head")
        prefix = f"repos/{repository}"
        pr = self._command_json(("/usr/bin/gh", "api", f"{prefix}/pulls/{pr_number}"))
        if str(pr.get("head", {}).get("sha") or "") != head:
            return {
                "actionable_version": version,
                "authority_status": "stale_head",
                "expected_head": head,
                "current_head": pr.get("head", {}).get("sha"),
                "pr_state": pr.get("state"),
            }
        checks = self._command_json(("/usr/bin/gh", "api", f"{prefix}/commits/{head}/check-runs?per_page=100"))
        statuses = self._command_json(("/usr/bin/gh", "api", f"{prefix}/commits/{head}/status"))
        reviews = self._command_json(("/usr/bin/gh", "api", f"{prefix}/pulls/{pr_number}/reviews?per_page=100"))
        files = self._command_json(("/usr/bin/gh", "api", f"{prefix}/pulls/{pr_number}/files?per_page=100"))
        main = self._command_json(("/usr/bin/gh", "api", f"{prefix}/branches/main"))
        main_sha = str(main.get("commit", {}).get("sha") or "")
        main_checks = self._command_json(("/usr/bin/gh", "api", f"{prefix}/commits/{main_sha}/check-runs?per_page=100"))
        def check_summary(value: Mapping[str, Any]) -> dict[str, Any]:
            output = value.get("output") if isinstance(value.get("output"), Mapping) else {}
            return {
                key: value.get(key)
                for key in ("id", "name", "status", "conclusion", "started_at", "completed_at", "details_url")
            } | {
                "output": {
                    "title": _clip(output.get("title")),
                    "summary": _clip(output.get("summary")),
                    "text": _clip(output.get("text")),
                }
            }
        return _bounded({
            "actionable_version": version,
            "authority_status": "current",
            "repository": repository,
            "pr": {
                "number": pr_number,
                "state": pr.get("state"),
                "draft": pr.get("draft"),
                "head": head,
                "base": pr.get("base", {}).get("ref"),
                "base_sha": pr.get("base", {}).get("sha"),
                "mergeable": pr.get("mergeable"),
            },
            "checks": [
                check_summary(value)
                for value in checks.get("check_runs") or [] if isinstance(value, Mapping)
            ],
            "statuses": [
                {key: value.get(key) for key in ("context", "state", "target_url", "description", "created_at")}
                for value in statuses.get("statuses") or [] if isinstance(value, Mapping)
            ],
            "reviews": [
                {
                    **{key: value.get(key) for key in ("id", "state", "commit_id", "submitted_at")},
                    "body": _clip(value.get("body")),
                }
                for value in reviews if isinstance(value, Mapping)
            ],
            "files": [
                {
                    **{key: value.get(key) for key in ("filename", "status", "additions", "deletions", "changes")},
                    "patch": _clip(value.get("patch"), limit=4_000),
                }
                for value in files if isinstance(value, Mapping)
            ],
            "current_main": {
                "sha": main_sha,
                "checks": [
                    check_summary(value)
                    for value in main_checks.get("check_runs") or [] if isinstance(value, Mapping)
                ],
            },
        })

    def get_exact_check_log(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        version = self._version(arguments)
        try:
            check_run_id = int(arguments.get("check_run_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("check_run_id must be a positive integer") from exc
        if check_run_id <= 0:
            raise ValueError("check_run_id must be a positive integer")
        case = self._case(version)["case"]
        repository = str(case.get("repository") or "")
        head = str(case.get("head") or case.get("reviewed_head") or "")
        if repository != self.repository or re.fullmatch(r"[0-9a-f]{40}", head) is None:
            raise ValueError("wake packet has no allowlisted exact PR head")
        prefix = f"repos/{repository}"
        check = self._command_json(("/usr/bin/gh", "api", f"{prefix}/check-runs/{check_run_id}"))
        if str(check.get("head_sha") or "") != head:
            raise ValueError("check run does not belong to the exact wake head")
        raw = self._command_text(("/usr/bin/gh", "api", f"{prefix}/actions/jobs/{check_run_id}/logs"))
        lines = raw.splitlines()
        interesting = re.compile(r"(?:error|fail(?:ed|ure)?|traceback|exception|assert|timed?\s*out)", re.I)
        selected_indexes: set[int] = set()
        for index, line in enumerate(lines):
            if interesting.search(line):
                selected_indexes.update(range(max(0, index - 2), min(len(lines), index + 3)))
            if len(selected_indexes) >= 300:
                break
        selected = [lines[index] for index in sorted(selected_indexes)[:300]]
        tail = lines[-80:]
        return _bounded({
            "actionable_version": version,
            "check_run_id": check_run_id,
            "check_name": check.get("name"),
            "head": head,
            "failure_focused_lines": selected,
            "tail_lines": tail,
            "raw_line_count": len(lines),
            "truncated": len(selected_indexes) > 300 or len(lines) > 80,
        })

    def get_repair_owner(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        version = self._version(arguments)
        resolved = self._case(version)
        decision = resolved.get("canonical_consumer_decision") or {}
        canonical = decision.get("canonical_ci") if isinstance(decision, Mapping) else {}
        task_gid = str(canonical.get("repair_owner_task") or "") if isinstance(canonical, Mapping) else ""
        if not task_gid:
            return {"actionable_version": version, "available": False, "reason": "no canonical repair owner"}
        if not task_gid.isdigit():
            raise ValueError("canonical repair owner is malformed")
        fields = (
            "gid,name,completed,modified_at,permalink_url,notes,"
            "memberships.project.gid,memberships.project.name,memberships.section.gid,memberships.section.name"
        )
        task = self._command_json((
            "/home/marco/.local/bin/asana",
            "raw",
            "GET",
            f"/tasks/{task_gid}?opt_fields={fields}",
        ))
        return _bounded({"actionable_version": version, "available": True, "task": task})

    def get_prior_integrator_decisions(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        version = self._version(arguments)
        matches: list[dict[str, Any]] = []
        for record in reversed(self._audit_records()):
            direct = record.get("actionable_version") == version
            listed = version in [str(value) for value in record.get("actionable_versions") or []]
            nested = any(
                isinstance(value, Mapping) and value.get("actionable_version") == version
                for value in record.get("decisions") or []
            )
            if direct or listed or nested:
                matched_decisions = [
                    dict(value)
                    for value in record.get("decisions") or []
                    if isinstance(value, Mapping) and value.get("actionable_version") == version
                ]
                matches.append(_bounded({
                    key: record.get(key)
                    for key in (
                        "at", "event", "wake_id", "actionable_version", "actionable_versions",
                        "receipt_status", "valid", "proposal", "reason", "result",
                    )
                    if record.get(key) is not None
                } | ({"decisions": matched_decisions} if matched_decisions else {})))
            if len(matches) == 20:
                break
        return {"actionable_version": version, "records": matches}

    def get_nightly_health(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ValueError("get_nightly_health accepts no arguments")
        prefix = f"repos/{self.repository}"
        main = self._command_json(("/usr/bin/gh", "api", f"{prefix}/branches/main"))
        runs = self._command_json((
            "/usr/bin/gh",
            "api",
            f"{prefix}/actions/workflows/full-regression.yml/runs?per_page=10",
        ))
        return _bounded({
            "repository": self.repository,
            "scheduler_owner": "GitHub Actions full-regression.yml",
            "observe_only": True,
            "current_main_sha": main.get("commit", {}).get("sha"),
            "runs": [
                {key: value.get(key) for key in (
                    "id", "event", "status", "conclusion", "head_sha", "run_attempt",
                    "created_at", "updated_at", "html_url",
                )}
                for value in runs.get("workflow_runs") or [] if isinstance(value, Mapping)
            ],
        })

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "get_integrator_case": self.get_integrator_case,
            "get_exact_pr_evidence": self.get_exact_pr_evidence,
            "get_exact_check_log": self.get_exact_check_log,
            "get_repair_owner": self.get_repair_owner,
            "get_prior_integrator_decisions": self.get_prior_integrator_decisions,
            "get_nightly_health": self.get_nightly_health,
        }
        if name not in handlers:
            raise ValueError("unknown Integrator tool")
        try:
            result = handlers[name](arguments)
        except Exception as exc:
            self.audit.write(
                "model_tool_call",
                tool=name,
                actionable_version=arguments.get("actionable_version"),
                result="refused",
                error_type=type(exc).__name__,
                model_turns_started=0,
            )
            raise
        else:
            self.audit.write(
                "model_tool_call",
                tool=name,
                actionable_version=arguments.get("actionable_version"),
                result="ok",
                model_turns_started=0,
            )
            return result


def _reply(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(value), separators=(",", ":")) + "\n")
    sys.stdout.flush()


def serve(tools: IntegratorReadTools) -> int:
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                continue
            request_id = request.get("id")
            if request_id is None:
                continue
            method = str(request.get("method") or "")
            params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
            if method == "initialize":
                result = {
                    "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "dish-integrator-read-tools", "version": "1"},
                    "instructions": (
                        "Read-only CI evidence for exact Lifecycle V4 actionable versions. "
                        "These tools never classify, mutate, rerun, dispatch, review, or merge."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(TOOLS)}
            elif method == "tools/call":
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
                structured = tools.call(name, arguments)
                result = {
                    "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
                    "structuredContent": structured,
                    "isError": False,
                }
            else:
                _reply({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                })
                continue
            _reply({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            request_id = request.get("id") if isinstance(request, Mapping) else None
            _reply({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"read refused: {type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    return serve(IntegratorReadTools(state_dir=args.state_dir, repository=args.repository))


if __name__ == "__main__":
    raise SystemExit(main())
