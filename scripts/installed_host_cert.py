#!/usr/bin/env python3
"""Exact-head installed Claude/Codex host-certification evidence helpers."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

REPOSITORY = "marcogallotta/ai-tools"
SCHEMA = "dish-installed-host-cert-v1"
MARKER = "dish-installed-host-cert:v1"
EVIDENCE = "exact-head installed Claude/Codex host certification"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MARKER_RE = re.compile(
    rf"<!--\s*{re.escape(MARKER)}\s+head=(?P<head>[0-9a-f]{{40}})\s+"
    r"result=(?P<result>pass|fail)\s+hosts=(?P<hosts>[a-z,]+)\s+"
    r"digest=(?P<digest>[0-9a-f]{64})\s*-->"
)
CERT_BLOCK_RE = re.compile(
    r"INSTALLED HOST CERTIFICATE\s*\n```json\s*\n(?P<json>.*?)\n```",
    re.DOTALL,
)


class HostCertError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostCertRequirement:
    hosts: tuple[str, ...]
    paths: tuple[str, ...]

    def json(self) -> dict[str, Any]:
        return {"hosts": list(self.hosts), "paths": list(self.paths)}


@dataclass(frozen=True)
class HostCertStatus:
    passed: bool
    error: str | None = None
    certificate: dict[str, Any] | None = None
    comment_id: int | None = None


def _filename(item: Mapping[str, Any]) -> str:
    return str(item.get("filename") or item.get("path") or "").strip()


def _install_wiring_patch(item: Mapping[str, Any]) -> bool:
    patch = str(item.get("patch") or "")
    if not patch:
        return False
    needles = (
        "/.claude/",
        "~/.claude/",
        "/.codex/",
        "~/.codex/",
        "/.local/bin/agent-reground",
        "/.local/bin/codex-protected-checkout",
        "ln -s",
    )
    return any(needle in patch for needle in needles)


def requirement_for_files(files: Iterable[Mapping[str, Any]]) -> HostCertRequirement | None:
    """Classify only runtime hook/config/install-wiring surfaces that cross the installed-host boundary."""
    hosts: set[str] = set()
    paths: set[str] = set()
    for item in files:
        path = _filename(item)
        if not path:
            continue
        path_hosts: set[str] = set()
        if path == ".claude/settings.json":
            path_hosts.add("claude")
        elif path == "codex/hooks.json":
            path_hosts.add("codex")
        elif path.startswith("hooks/") and not path.startswith("hooks/tests/"):
            path_hosts.update(("claude", "codex"))
        elif path in {"README.md", "codex/README.md"} and _install_wiring_patch(item):
            # Installation wiring is operational even when expressed in the repo-owned install runbook.
            if ".claude" in str(item.get("patch") or ""):
                path_hosts.add("claude")
            if ".codex" in str(item.get("patch") or "") or "codex" in path:
                path_hosts.add("codex")
            if "/.local/bin/" in str(item.get("patch") or ""):
                path_hosts.update(("claude", "codex"))
        if path_hosts:
            paths.add(path)
            hosts.update(path_hosts)
    if not hosts:
        return None
    return HostCertRequirement(tuple(sorted(hosts)), tuple(sorted(paths)))


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def certificate_digest(certificate: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(certificate)).hexdigest()


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HostCertError(f"certificate {label} must be a non-empty string")
    return value.strip()


def _require_bool(value: Any, label: str) -> bool:
    if value is not True:
        raise HostCertError(f"certificate {label} must be true")
    return True


def _require_digest(value: Any, label: str) -> str:
    text = _require_string(value, label).lower()
    if DIGEST_RE.fullmatch(text) is None:
        raise HostCertError(f"certificate {label} must be a SHA-256 hex digest")
    return text


def _require_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise HostCertError(f"certificate {label} must be a non-empty string list")
    return [str(item) for item in value]


def _validate_identity(identity: Any, *, branch: str, head: str, pr_number: int, task_ids: list[str]) -> None:
    if not isinstance(identity, dict):
        raise HostCertError("certificate identity must be an object")
    _require_string(identity.get("agent_id"), "identity.agent_id")
    if identity.get("host") not in {"claude", "codex"}:
        raise HostCertError("certificate identity.host must be claude or codex")
    if identity.get("source") != "launch-provenance":
        raise HostCertError("certificate identity.source must be launch-provenance")
    _require_string(identity.get("claim_id"), "identity.claim_id")
    if str(identity.get("branch")) != branch:
        raise HostCertError("certificate identity branch does not match candidate")
    if str(identity.get("pr_head")) != head or int(identity.get("pr_number") or 0) != pr_number:
        raise HostCertError("certificate identity PR/head does not match candidate")
    if str(identity.get("task_gid")) not in task_ids:
        raise HostCertError("certificate identity task is not one of the candidate owning tasks")
    _require_string(identity.get("launch_id"), "identity.launch_id")


def _parse_timestamp(value: Any, label: str) -> dt.datetime:
    raw = _require_string(value, label).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HostCertError(f"certificate {label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise HostCertError(f"certificate {label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _validate_fence(fence: Any, *, required_hosts: list[str]) -> None:
    if not isinstance(fence, dict):
        raise HostCertError("certificate fence must be an object")
    if fence.get("window") != "full":
        raise HostCertError("certificate fence.window must be full")
    mechanism = fence.get("mechanism")
    if mechanism not in {"exclusive-shared-host-fence", "isolated-host-state"}:
        raise HostCertError(
            "certificate fence.mechanism must be exclusive-shared-host-fence or isolated-host-state"
        )
    if mechanism == "exclusive-shared-host-fence":
        _require_string(fence.get("fence_id"), "fence.fence_id")
    else:
        _require_digest(fence.get("isolation_proof_digest"), "fence.isolation_proof_digest")
    producers = set(_require_string_list(fence.get("producer_classes"), "fence.producer_classes"))
    missing = set(required_hosts) - producers
    if missing or "host-config-writer" not in producers:
        detail = ", ".join(sorted(missing | ({"host-config-writer"} - producers)))
        raise HostCertError(f"certificate fence is missing affected producer/consumer classes: {detail}")
    _require_digest(fence.get("pre_state_digest"), "fence.pre_state_digest")
    _require_digest(fence.get("final_state_digest"), "fence.final_state_digest")
    if fence.get("concurrent_change_detected") is not False:
        raise HostCertError("certificate fence must prove no concurrent host-config change")
    started = _parse_timestamp(fence.get("started_at"), "fence.started_at")
    ended = _parse_timestamp(fence.get("ended_at"), "fence.ended_at")
    if ended < started:
        raise HostCertError("certificate fence.ended_at precedes fence.started_at")


def _validate_host_result(item: Any, required_host: str) -> None:
    if not isinstance(item, dict) or item.get("host") != required_host:
        raise HostCertError(f"certificate host result for {required_host} is missing")
    _require_string(item.get("version"), f"hosts.{required_host}.version")
    _require_string(item.get("binary"), f"hosts.{required_host}.binary")
    _require_string_list(item.get("effective_config_sources"), f"hosts.{required_host}.effective_config_sources")
    active_paths = item.get("active_paths")
    if not isinstance(active_paths, list) or not active_paths:
        raise HostCertError(f"certificate hosts.{required_host}.active_paths must be non-empty")
    for index, path in enumerate(active_paths):
        if not isinstance(path, dict):
            raise HostCertError(f"certificate hosts.{required_host}.active_paths[{index}] must be an object")
        _require_string(path.get("path"), f"hosts.{required_host}.active_paths[{index}].path")
        _require_string(path.get("resolved_target"), f"hosts.{required_host}.active_paths[{index}].resolved_target")
        _require_digest(path.get("sha256"), f"hosts.{required_host}.active_paths[{index}].sha256")
    loader = item.get("loader_execution")
    if not isinstance(loader, dict):
        raise HostCertError(f"certificate hosts.{required_host}.loader_execution must be an object")
    _require_bool(loader.get("actual_installed_binary"), f"hosts.{required_host}.loader_execution.actual_installed_binary")
    if loader.get("result") != "pass":
        raise HostCertError(f"certificate hosts.{required_host}.loader_execution must pass")
    if item.get("harmless_governed_action") != "pass":
        raise HostCertError(f"certificate hosts.{required_host}.harmless_governed_action must pass")
    if item.get("deliberate_conflict") != "denied":
        raise HostCertError(f"certificate hosts.{required_host}.deliberate_conflict must be denied")


def validate_certificate(
    certificate: Mapping[str, Any],
    *,
    repository: str,
    pr_number: int,
    branch: str,
    head: str,
    task_ids: Iterable[str],
    requirement: HostCertRequirement,
) -> dict[str, Any]:
    if certificate.get("schema") != SCHEMA:
        raise HostCertError(f"certificate schema must be {SCHEMA}")
    if certificate.get("repository") != repository:
        raise HostCertError("certificate repository does not match candidate")
    if int(certificate.get("pr_number") or 0) != pr_number:
        raise HostCertError("certificate PR number does not match candidate")
    if certificate.get("branch") != branch or certificate.get("head") != head:
        raise HostCertError("certificate branch/head does not match candidate")
    tasks = _require_string_list(certificate.get("task_ids"), "task_ids")
    expected_tasks = sorted(str(item) for item in task_ids)
    if sorted(tasks) != expected_tasks:
        raise HostCertError("certificate task_ids do not match candidate owning tasks")
    required_hosts = sorted(requirement.hosts)
    if sorted(_require_string_list(certificate.get("required_hosts"), "required_hosts")) != required_hosts:
        raise HostCertError("certificate required_hosts do not match changed-surface classification")
    changed_paths = sorted(_require_string_list(certificate.get("changed_paths"), "changed_paths"))
    if changed_paths != sorted(requirement.paths):
        raise HostCertError("certificate changed_paths do not match changed-surface classification")

    _validate_identity(certificate.get("identity"), branch=branch, head=head, pr_number=pr_number, task_ids=tasks)
    fence = certificate.get("fence")
    _validate_fence(fence, required_hosts=required_hosts)

    host_results = certificate.get("hosts")
    if not isinstance(host_results, list):
        raise HostCertError("certificate hosts must be a list")
    if not all(isinstance(item, dict) for item in host_results):
        raise HostCertError("certificate hosts entries must be objects")
    result_hosts = [str(item.get("host")) for item in host_results]
    if len(result_hosts) != len(set(result_hosts)):
        raise HostCertError("certificate hosts must not contain duplicate host results")
    if sorted(result_hosts) != required_hosts:
        raise HostCertError("certificate hosts must exactly match required_hosts")
    by_host = {str(item.get("host")): item for item in host_results}
    for host in required_hosts:
        _validate_host_result(by_host.get(host), host)

    checks = certificate.get("checks")
    if not isinstance(checks, dict):
        raise HostCertError("certificate checks must be an object")
    for key in (
        "unidentified_session_fails_closed",
        "compaction_recovery",
        "broken_asana_recovery",
        "worktree_prerequisites",
        "shell_config_trust",
        "no_stale_removed_references",
        "effective_config_parity",
        "head_movement_invalidation",
        "security_decision_boundary",
    ):
        _require_bool(checks.get(key), f"checks.{key}")

    disposition = certificate.get("disposition")
    if not isinstance(disposition, dict):
        raise HostCertError("certificate disposition must be an object")
    mode = disposition.get("mode")
    if mode not in {"temporary-restored", "final-activated"}:
        raise HostCertError("certificate disposition.mode must be temporary-restored or final-activated")
    if disposition.get("readback") != "pass":
        raise HostCertError("certificate disposition.readback must pass")
    readback_digest = _require_digest(disposition.get("readback_digest"), "disposition.readback_digest")
    assert isinstance(fence, dict)
    final_digest = _require_digest(fence.get("final_state_digest"), "fence.final_state_digest")
    if readback_digest != final_digest:
        raise HostCertError("certificate disposition readback digest does not match final host state")
    if mode == "temporary-restored":
        pre_digest = _require_digest(fence.get("pre_state_digest"), "fence.pre_state_digest")
        if final_digest != pre_digest:
            raise HostCertError("temporary certification did not restore the exact prior host state")
    else:
        _require_string(disposition.get("activation_authority"), "disposition.activation_authority")
    return dict(certificate)


def _comment_sort_key(comment: Mapping[str, Any]) -> tuple[str, int]:
    timestamp = str(comment.get("updated_at") or comment.get("created_at") or "")
    try:
        cid = int(comment.get("id") or 0)
    except (TypeError, ValueError):
        cid = 0
    return timestamp, cid


def status_from_comments(
    comments: Iterable[Mapping[str, Any]],
    *,
    repository: str,
    pr_number: int,
    branch: str,
    head: str,
    task_ids: Iterable[str],
    requirement: HostCertRequirement,
) -> HostCertStatus:
    candidates: list[Mapping[str, Any]] = []
    exact_head_token = f"head={head}"
    for comment in comments:
        body = str(comment.get("body") or "")
        if MARKER in body and exact_head_token in body:
            candidates.append(comment)
    if not candidates:
        return HostCertStatus(False, "no exact-head installed-host certificate")
    comment = max(candidates, key=_comment_sort_key)
    body = str(comment.get("body") or "")
    marker_match = next((m for m in MARKER_RE.finditer(body) if m.group("head") == head), None)
    if marker_match is None:
        return HostCertStatus(
            False,
            "latest exact-head installed-host certificate marker is malformed",
            comment_id=int(comment.get("id") or 0),
        )
    if marker_match.group("result") != "pass":
        return HostCertStatus(False, "latest exact-head installed-host certificate reports failure", comment_id=int(comment.get("id") or 0))
    marker_hosts = sorted(filter(None, marker_match.group("hosts").split(",")))
    if marker_hosts != sorted(requirement.hosts):
        return HostCertStatus(False, "installed-host certificate marker host set does not match changed surface", comment_id=int(comment.get("id") or 0))
    block = CERT_BLOCK_RE.search(body, marker_match.end())
    if block is None:
        return HostCertStatus(False, "installed-host certificate JSON block is missing", comment_id=int(comment.get("id") or 0))
    try:
        certificate = json.loads(block.group("json"))
    except json.JSONDecodeError as exc:
        return HostCertStatus(False, f"installed-host certificate JSON is invalid: {exc}", comment_id=int(comment.get("id") or 0))
    if not isinstance(certificate, dict):
        return HostCertStatus(False, "installed-host certificate JSON must be an object", comment_id=int(comment.get("id") or 0))
    digest = certificate_digest(certificate)
    if digest != marker_match.group("digest"):
        return HostCertStatus(False, "installed-host certificate digest does not match marker", comment_id=int(comment.get("id") or 0))
    try:
        validated = validate_certificate(
            certificate,
            repository=repository,
            pr_number=pr_number,
            branch=branch,
            head=head,
            task_ids=task_ids,
            requirement=requirement,
        )
    except HostCertError as exc:
        return HostCertStatus(False, str(exc), comment_id=int(comment.get("id") or 0))
    return HostCertStatus(True, certificate=validated, comment_id=int(comment.get("id") or 0))


def render_comment(certificate: Mapping[str, Any]) -> str:
    hosts = _require_string_list(certificate.get("required_hosts"), "required_hosts")
    head = _require_string(certificate.get("head"), "head").lower()
    if FULL_SHA_RE.fullmatch(head) is None:
        raise HostCertError("certificate head must be an exact SHA")
    digest = certificate_digest(certificate)
    marker = f"<!-- {MARKER} head={head} result=pass hosts={','.join(sorted(hosts))} digest={digest} -->"
    rendered = json.dumps(certificate, indent=2, sort_keys=True, ensure_ascii=False)
    return f"{marker}\nINSTALLED HOST CERTIFICATE\n```json\n{rendered}\n```"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="render a durable PR comment from a certificate JSON file")
    render.add_argument("certificate")
    digest = sub.add_parser("digest", help="print canonical certificate SHA-256")
    digest.add_argument("certificate")
    args = parser.parse_args(argv)
    value = json.loads(Path(args.certificate).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HostCertError("certificate file must contain a JSON object")
    if args.command == "render":
        print(render_comment(value))
    else:
        print(certificate_digest(value))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (HostCertError, OSError, json.JSONDecodeError) as exc:
        print(f"installed-host-cert: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
