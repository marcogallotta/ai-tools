#!/usr/bin/env python3
"""Exact-head installed Claude/Codex host-certification evidence helpers."""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import re
import shlex
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
    active_paths: tuple[tuple[str, tuple[str, ...]], ...] = ()
    candidate_blobs: tuple[tuple[str, str], ...] = ()

    def json(self) -> dict[str, Any]:
        return {
            "hosts": list(self.hosts),
            "paths": list(self.paths),
            "active_paths": {host: list(paths) for host, paths in self.active_paths},
            "candidate_blobs": dict(self.candidate_blobs),
        }


@dataclass(frozen=True)
class HostCertStatus:
    passed: bool
    error: str | None = None
    certificate: dict[str, Any] | None = None
    comment_id: int | None = None


def _filename(item: Mapping[str, Any]) -> str:
    return str(item.get("filename") or item.get("path") or "").strip()


def _repo_root(repo_root: Path | str | None = None) -> Path:
    if repo_root is not None:
        return Path(repo_root).resolve()
    return Path(__file__).resolve().parents[1]


def _hook_commands(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str) and command.strip():
            yield command.strip()
        for child in value.values():
            yield from _hook_commands(child)
    elif isinstance(value, list):
        for child in value:
            yield from _hook_commands(child)


def _command_hook_path(command: str, root: Path) -> str | None:
    match = re.search(r"(?:\$CLAUDE_PROJECT_DIR|[^\s\"']*)/hooks/(?P<name>[A-Za-z0-9_.-]+)", command)
    if match:
        return f"hooks/{match.group('name')}"
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    hooks = root / "hooks"
    for token in tokens:
        name = Path(token).name
        if name and (hooks / name).is_file():
            return f"hooks/{name}"
    return None


def _python_hook_dependencies(root: Path, path: str) -> set[str]:
    source_path = root / path
    try:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import) and len(node.names) == 1:
            module = node.names[0].name
        if not module or "." in module:
            continue
        candidate = root / "hooks" / f"{module}.py"
        if candidate.is_file():
            dependencies.add(candidate.relative_to(root).as_posix())
    return dependencies


def active_hook_surface(repo_root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Derive the active hook surface from authoritative Claude/Codex configs.

    Direct host adapters come only from `.claude/settings.json` and `codex/hooks.json`.
    Python modules imported by those adapters are marked as active components, but they
    are not host protocol boundaries by themselves.
    """
    root = _repo_root(repo_root)
    direct: dict[str, set[str]] = {}
    configs = {"claude": ".claude/settings.json", "codex": "codex/hooks.json"}
    for host, config_path in configs.items():
        try:
            config = json.loads((root / config_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HostCertError(f"cannot derive {host} active hooks from {config_path}: {exc}") from exc
        for command in _hook_commands(config):
            hook_path = _command_hook_path(command, root)
            if hook_path:
                direct.setdefault(hook_path, set()).add(host)

    components: dict[str, set[str]] = {}
    frontier = list(direct)
    seen = set(frontier)
    while frontier:
        parent = frontier.pop()
        parent_hosts = direct.get(parent) or components.get(parent) or set()
        for dependency in _python_hook_dependencies(root, parent):
            components.setdefault(dependency, set()).update(parent_hosts)
            if dependency not in seen:
                seen.add(dependency)
                frontier.append(dependency)

    result: dict[str, dict[str, Any]] = {}
    for path, hosts in sorted(direct.items()):
        result[path] = {"hosts": tuple(sorted(hosts)), "boundary": "host-adapter"}
    for path, hosts in sorted(components.items()):
        if path in result:
            continue
        result[path] = {"hosts": tuple(sorted(hosts)), "boundary": "active-component"}
    return result


def hook_surface_classification(path: str, repo_root: Path | str | None = None) -> str | None:
    root = _repo_root(repo_root)
    surface = active_hook_surface(root)
    if path in {".claude/settings.json", "codex/hooks.json"}:
        return "INSTALL_WIRING"
    item = surface.get(path)
    if item:
        hosts = set(item["hosts"])
        if hosts == {"claude", "codex"}:
            return "SHARED_ACTIVE"
        if hosts == {"claude"}:
            return "CLAUDE_ACTIVE"
        if hosts == {"codex"}:
            return "CODEX_ACTIVE"
    if path.startswith("hooks/") and not path.startswith("hooks/tests/"):
        return "DORMANT_COMPONENT"
    return None


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


def requirement_for_files(
    files: Iterable[Mapping[str, Any]], repo_root: Path | str | None = None
) -> HostCertRequirement | None:
    """Classify only runtime hook/config/install-wiring surfaces that cross the installed-host boundary."""
    root = _repo_root(repo_root)
    surface = active_hook_surface(root)
    hosts: set[str] = set()
    paths: set[str] = set()
    candidate_blobs: dict[str, str] = {}
    harness_paths = {"scripts/installed_host_cert.py", "tools/dish-hook-certify"}
    for item in files:
        path = _filename(item)
        if not path:
            continue
        path_hosts: set[str] = set()
        if path == ".claude/settings.json":
            path_hosts.add("claude")
        elif path == "codex/hooks.json":
            path_hosts.add("codex")
        elif path in surface and surface[path]["boundary"] == "host-adapter":
            path_hosts.update(surface[path]["hosts"])
        elif path in harness_paths:
            # The shared one-command certificate harness is itself a concrete host boundary.
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
            blob = str(item.get("sha") or "").strip().lower()
            if FULL_SHA_RE.fullmatch(blob):
                candidate_blobs[path] = blob
    if not hosts:
        return None
    active_paths = tuple(
        (host, tuple(sorted(path for path, item in surface.items() if item["boundary"] == "host-adapter" and host in item["hosts"])))
        for host in sorted(hosts)
    )
    return HostCertRequirement(
        tuple(sorted(hosts)),
        tuple(sorted(paths)),
        active_paths,
        tuple(sorted(candidate_blobs.items())),
    )


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


def _require_git_blob(value: Any, label: str) -> str:
    text = _require_string(value, label).lower()
    if FULL_SHA_RE.fullmatch(text) is None:
        raise HostCertError(f"certificate {label} must be a 40-character Git blob SHA")
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


def _validate_host_result(
    item: Any,
    required_host: str,
    *,
    expected_active_paths: Iterable[str],
    candidate_blobs: Mapping[str, str],
    candidate_file_evidence: Mapping[str, tuple[str, str]],
) -> None:
    if not isinstance(item, dict) or item.get("host") != required_host:
        raise HostCertError(f"certificate host result for {required_host} is missing")
    _require_string(item.get("version"), f"hosts.{required_host}.version")
    _require_string(item.get("binary"), f"hosts.{required_host}.binary")
    candidate_root = Path(_require_string(item.get("candidate_root"), f"hosts.{required_host}.candidate_root"))
    if not candidate_root.is_absolute():
        raise HostCertError(f"certificate hosts.{required_host}.candidate_root must be absolute")

    config_path = ".claude/settings.json" if required_host == "claude" else "codex/hooks.json"
    effective_sources = item.get("effective_config_sources")
    if not isinstance(effective_sources, list) or not effective_sources:
        raise HostCertError(f"certificate hosts.{required_host}.effective_config_sources must be non-empty")
    config_match = False
    for index, source in enumerate(effective_sources):
        label = f"hosts.{required_host}.effective_config_sources[{index}]"
        if not isinstance(source, dict):
            raise HostCertError(f"certificate {label} must be an object with candidate-byte evidence")
        _require_string(source.get("path"), f"{label}.path")
        _require_string(source.get("resolved_target"), f"{label}.resolved_target")
        candidate_path = _require_string(source.get("candidate_path"), f"{label}.candidate_path")
        blob = _require_git_blob(source.get("git_blob_sha"), f"{label}.git_blob_sha")
        actual_digest = _require_digest(source.get("sha256"), f"{label}.sha256")
        candidate_digest = _require_digest(source.get("candidate_sha256"), f"{label}.candidate_sha256")
        transform = source.get("transform")
        if transform not in {"none", "absolute-command-rebase"}:
            raise HostCertError(f"certificate {label}.transform must be none or absolute-command-rebase")
        if transform == "none" and actual_digest != candidate_digest:
            raise HostCertError(f"certificate {label} does not match exact candidate config bytes")
        if transform == "none":
            expected_target = candidate_root.joinpath(*Path(candidate_path).parts)
            if Path(str(source.get("resolved_target"))).resolve(strict=False) != expected_target.resolve(strict=False):
                raise HostCertError(f"certificate {label}.resolved_target is not the exact candidate config path")
        if transform == "absolute-command-rebase" and required_host != "codex":
            raise HostCertError(f"certificate {label} may rebase commands only for the Codex isolated config")
        if transform == "absolute-command-rebase":
            command_targets = source.get("active_command_targets")
            if not isinstance(command_targets, list) or not command_targets:
                raise HostCertError(f"certificate {label}.active_command_targets must be non-empty")
            normalized_targets = {str(Path(_require_string(value, f"{label}.active_command_targets"))) for value in command_targets}
            expected_targets = {
                str(candidate_root.joinpath(*Path(path).parts)) for path in expected_active_paths
            }
            if normalized_targets != expected_targets:
                raise HostCertError(
                    f"certificate {label}.active_command_targets do not resolve exactly to the candidate hook surface"
                )
        expected_blob = candidate_blobs.get(candidate_path)
        if expected_blob and blob != expected_blob:
            raise HostCertError(f"certificate {label} Git blob does not match exact candidate")
        candidate_evidence = candidate_file_evidence.get(candidate_path)
        if candidate_evidence and (blob, candidate_digest) != candidate_evidence:
            raise HostCertError(f"certificate {label} candidate bytes do not match candidate_files evidence")
        if candidate_path == config_path:
            config_match = True
    if not config_match:
        raise HostCertError(
            f"certificate hosts.{required_host}.effective_config_sources does not bind {config_path}"
        )

    active_paths = item.get("active_paths")
    if not isinstance(active_paths, list) or not active_paths:
        raise HostCertError(f"certificate hosts.{required_host}.active_paths must be non-empty")
    seen_candidate_paths: set[str] = set()
    for index, path in enumerate(active_paths):
        if not isinstance(path, dict):
            raise HostCertError(f"certificate hosts.{required_host}.active_paths[{index}] must be an object")
        label = f"hosts.{required_host}.active_paths[{index}]"
        _require_string(path.get("path"), f"{label}.path")
        resolved_target = Path(_require_string(path.get("resolved_target"), f"{label}.resolved_target"))
        candidate_path = _require_string(path.get("candidate_path"), f"{label}.candidate_path")
        if candidate_path.startswith("/") or ".." in Path(candidate_path).parts:
            raise HostCertError(f"certificate {label}.candidate_path must be repository-relative")
        expected_target = candidate_root.joinpath(*Path(candidate_path).parts)
        if Path(str(resolved_target)).resolve(strict=False) != expected_target.resolve(strict=False):
            raise HostCertError(f"certificate {label}.resolved_target is not the exact candidate path")
        blob = _require_git_blob(path.get("git_blob_sha"), f"{label}.git_blob_sha")
        _require_digest(path.get("sha256"), f"{label}.sha256")
        expected_blob = candidate_blobs.get(candidate_path)
        if expected_blob and blob != expected_blob:
            raise HostCertError(f"certificate {label} Git blob does not match exact candidate")
        candidate_evidence = candidate_file_evidence.get(candidate_path)
        if candidate_evidence and (blob, _require_digest(path.get("sha256"), f"{label}.sha256")) != candidate_evidence:
            raise HostCertError(f"certificate {label} bytes do not match candidate_files evidence")
        seen_candidate_paths.add(candidate_path)
    missing_active = set(expected_active_paths) - seen_candidate_paths
    if missing_active:
        raise HostCertError(
            f"certificate hosts.{required_host}.active_paths missing candidate hook(s): "
            + ", ".join(sorted(missing_active))
        )
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
    candidate_files = certificate.get("candidate_files")
    if not isinstance(candidate_files, list) or not candidate_files:
        raise HostCertError("certificate candidate_files must be a non-empty list")
    expected_blobs = dict(requirement.candidate_blobs)
    seen_candidate_files: dict[str, tuple[str, str]] = {}
    for index, item in enumerate(candidate_files):
        label = f"candidate_files[{index}]"
        if not isinstance(item, dict):
            raise HostCertError(f"certificate {label} must be an object")
        path = _require_string(item.get("path"), f"{label}.path")
        blob = _require_git_blob(item.get("git_blob_sha"), f"{label}.git_blob_sha")
        digest = _require_digest(item.get("sha256"), f"{label}.sha256")
        seen_candidate_files[path] = (blob, digest)
    for path, blob in expected_blobs.items():
        if seen_candidate_files.get(path, (None, None))[0] != blob:
            raise HostCertError(f"certificate candidate_files does not bind exact candidate blob for {path}")

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
    active_paths_by_host = dict(requirement.active_paths)
    for host in required_hosts:
        config_path = ".claude/settings.json" if host == "claude" else "codex/hooks.json"
        missing_candidate_evidence = set(active_paths_by_host.get(host, ())) | {config_path}
        missing_candidate_evidence -= set(seen_candidate_files)
        if missing_candidate_evidence:
            raise HostCertError(
                "certificate candidate_files missing active candidate evidence: "
                + ", ".join(sorted(missing_candidate_evidence))
            )
        _validate_host_result(
            by_host.get(host),
            host,
            expected_active_paths=active_paths_by_host.get(host, ()),
            candidate_blobs=expected_blobs,
            candidate_file_evidence=seen_candidate_files,
        )

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
