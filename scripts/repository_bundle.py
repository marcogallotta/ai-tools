#!/usr/bin/env python3
"""Build and verify exact-SHA Git repository bundles for ChatGPT agents."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ARTIFACT_PREFIX = "repository-bundle-"
MAIN_REF = "refs/heads/main"
SHA_RE = re.compile(r"[0-9a-f]{40}")
PUBLICATION_EVENTS = {"push", "workflow_dispatch"}


class BundleError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if completed.returncode != 0:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        detail = f"\nstdout:\n{stdout}\nstderr:\n{stderr}" if capture else ""
        raise BundleError(f"command failed ({completed.returncode}): {' '.join(command)}{detail}")
    return completed


def _git(repo_root: Path, *args: str) -> str:
    return _run(["git", "-C", str(repo_root), *args]).stdout.strip()


def _require_sha(value: str, label: str) -> str:
    value = value.strip().lower()
    if not SHA_RE.fullmatch(value):
        raise BundleError(f"{label} must be an exact 40-character lowercase Git SHA")
    return value


def _require_main_ref(value: str) -> str:
    if value != MAIN_REF:
        raise BundleError(f"v1 repository bundles support only {MAIN_REF}; got {value!r}")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BundleError(f"JSON object required: {path}")
    return value


def _artifact_metadata(source_sha: str) -> dict[str, str]:
    artifact_name = f"{ARTIFACT_PREFIX}{source_sha}"
    bundle_name = f"{artifact_name}.bundle"
    return {
        "artifact_name": artifact_name,
        "release_tag": artifact_name,
        "bundle_name": bundle_name,
        "manifest_name": f"{artifact_name}.manifest.json",
        "checksum_name": f"{bundle_name}.sha256",
    }


def _parse_bundle_heads(text: str) -> list[dict[str, str]]:
    heads: list[dict[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            sha, ref = line.split(maxsplit=1)
        except ValueError as exc:
            raise BundleError(f"cannot parse git bundle head: {raw_line!r}") from exc
        heads.append({"sha": _require_sha(sha, "advertised SHA"), "ref": ref})
    heads.sort(key=lambda item: item["ref"])
    return heads


def _bundle_heads(bundle: Path) -> list[dict[str, str]]:
    return _parse_bundle_heads(_run(["git", "bundle", "list-heads", str(bundle)]).stdout)


def _authority_sha(repo_root: Path, source_ref: str) -> str:
    output = _git(repo_root, "ls-remote", "--exit-code", "origin", source_ref)
    rows = [line for line in output.splitlines() if line.strip()]
    if len(rows) != 1:
        raise BundleError(f"GitHub authority returned {len(rows)} rows for {source_ref}; expected exactly one")
    sha, ref = rows[0].split(maxsplit=1)
    if ref != source_ref:
        raise BundleError(f"GitHub authority ref mismatch: expected {source_ref!r}, got {ref!r}")
    return _require_sha(sha, "authority SHA")


def verify_authority(repo_root: Path, source_sha: str, source_ref: str) -> None:
    source_sha = _require_sha(source_sha, "source SHA")
    source_ref = _require_main_ref(source_ref)
    authority_sha = _authority_sha(repo_root, source_ref)
    if authority_sha != source_sha:
        raise BundleError(
            f"source SHA is stale or mismatched: GitHub authority has {authority_sha}, requested {source_sha}"
        )
    local_main = _git(repo_root, "rev-parse", f"{source_ref}^{{commit}}")
    if local_main != source_sha:
        raise BundleError(f"local advertised main mismatch: {local_main} != {source_sha}")
    _git(repo_root, "cat-file", "-e", f"{source_sha}^{{commit}}")


def verify_publication_event(
    repo_root: Path,
    source_sha: str,
    source_ref: str,
    event_name: str,
    event_sha: str,
    event_ref: str,
) -> dict[str, str]:
    source_sha = _require_sha(source_sha, "source SHA")
    source_ref = _require_main_ref(source_ref)
    event_sha = _require_sha(event_sha, "event SHA")
    event_ref = _require_main_ref(event_ref)
    if event_name not in PUBLICATION_EVENTS:
        raise BundleError(f"unsupported repository bundle publication event: {event_name!r}")
    if event_sha != source_sha:
        raise BundleError(f"workflow event SHA mismatch: {event_sha} != {source_sha}")
    if event_ref != source_ref:
        raise BundleError(f"workflow event ref mismatch: {event_ref!r} != {source_ref!r}")
    local_main = _git(repo_root, "rev-parse", f"{source_ref}^{{commit}}")
    if local_main != source_sha:
        raise BundleError(f"local advertised main mismatch: {local_main} != {source_sha}")
    _git(repo_root, "cat-file", "-e", f"{source_sha}^{{commit}}")
    return {"name": event_name, "sha": event_sha, "ref": event_ref}


def build_bundle(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    source_sha = _require_sha(args.source_sha, "source SHA")
    source_ref = _require_main_ref(args.source_ref)
    if not args.repository or "/" not in args.repository:
        raise BundleError("repository identity must be owner/name")

    event = verify_publication_event(
        repo_root,
        source_sha,
        source_ref,
        args.event_name,
        args.event_sha,
        args.event_ref,
    )
    metadata = _artifact_metadata(source_sha)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle = output_dir / metadata["bundle_name"]
    manifest_path = output_dir / metadata["manifest_name"]
    checksum_path = output_dir / metadata["checksum_name"]
    for path in (bundle, manifest_path, checksum_path):
        if path.exists():
            path.unlink()

    _run(["git", "-C", str(repo_root), "bundle", "create", str(bundle), source_ref])
    advertised_refs = _bundle_heads(bundle)
    expected_advertisement = {"sha": source_sha, "ref": source_ref}
    if expected_advertisement not in advertised_refs:
        raise BundleError(f"bundle does not advertise exact main: {expected_advertisement!r}")
    if any(item["ref"] == source_ref and item["sha"] != source_sha for item in advertised_refs):
        raise BundleError("bundle advertises main at the wrong SHA")

    bundle_sha256 = _sha256_file(bundle)
    checksum_path.write_text(f"{bundle_sha256}  {bundle.name}\n", encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_name": metadata["artifact_name"],
        "repository": {"full_name": args.repository, "id": str(args.repository_id)},
        "source": {"sha": source_sha, "ref": source_ref},
        "generator": {
            "workflow": args.workflow,
            "workflow_ref": args.workflow_ref,
            "workflow_sha": args.workflow_sha,
            "run_id": str(args.run_id),
            "run_attempt": str(args.run_attempt),
            "event": event,
        },
        "bundle": {
            "filename": bundle.name,
            "sha256": bundle_sha256,
            "format": "git-bundle-v1",
        },
        "checksum_filename": checksum_path.name,
        "advertised_refs": advertised_refs,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        **metadata,
        "bundle_sha256": bundle_sha256,
        "bundle_size_bytes": bundle.stat().st_size,
        "repository": args.repository,
        "repository_id": str(args.repository_id),
        "source_sha": source_sha,
        "source_ref": source_ref,
        "advertised_refs": advertised_refs,
    }


def _verify_checksum_file(checksum_path: Path, bundle: Path, expected_sha256: str) -> None:
    try:
        text = checksum_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BundleError(f"cannot read checksum {checksum_path}: {exc}") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  (.+)", text)
    if not match:
        raise BundleError("checksum file must use sha256sum format")
    if match.group(2) != bundle.name:
        raise BundleError(f"checksum filename target mismatch: {match.group(2)!r} != {bundle.name!r}")
    if match.group(1) != expected_sha256:
        raise BundleError("checksum file does not match manifest bundle checksum")
    actual = _sha256_file(bundle)
    if actual != expected_sha256:
        raise BundleError(f"bundle checksum mismatch: expected {expected_sha256}, got {actual}")


def _verify_bundle_command(bundle: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="repository-bundle-verify-") as temp_dir:
        bare = Path(temp_dir) / "verify.git"
        _run(["git", "init", "--bare", str(bare)])
        completed = _run(["git", "-C", str(bare), "bundle", "verify", str(bundle)])
        return (completed.stdout + completed.stderr).strip()


def verify_and_clone(args: argparse.Namespace) -> dict[str, Any]:
    bundle = Path(args.bundle).resolve()
    manifest_path = Path(args.manifest).resolve()
    checksum_path = Path(args.checksum).resolve()
    expected_sha = _require_sha(args.expected_sha, "expected SHA")
    expected_ref = _require_main_ref(args.expected_ref)
    manifest = _read_json(manifest_path)

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleError(f"manifest schema mismatch: {manifest.get('schema_version')!r}")
    metadata = _artifact_metadata(expected_sha)
    if manifest.get("artifact_name") != metadata["artifact_name"]:
        raise BundleError("manifest artifact identity does not match expected SHA")
    repository = manifest.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != args.expected_repository:
        raise BundleError("repository identity mismatch")
    if args.expected_repository_id is not None and str(repository.get("id")) != str(args.expected_repository_id):
        raise BundleError("repository numeric identity mismatch")
    source = manifest.get("source")
    if source != {"sha": expected_sha, "ref": expected_ref}:
        raise BundleError(f"manifest source mismatch: {source!r}")
    bundle_info = manifest.get("bundle")
    if not isinstance(bundle_info, dict):
        raise BundleError("manifest bundle object is missing")
    if bundle_info.get("filename") != bundle.name or bundle.name != metadata["bundle_name"]:
        raise BundleError("bundle filename does not match manifest/expected SHA")
    if manifest.get("checksum_filename") != checksum_path.name or checksum_path.name != metadata["checksum_name"]:
        raise BundleError("checksum filename does not match manifest/expected SHA")
    bundle_sha256 = str(bundle_info.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
        raise BundleError("manifest bundle sha256 is invalid")
    _verify_checksum_file(checksum_path, bundle, bundle_sha256)

    manifest_heads = manifest.get("advertised_refs")
    if not isinstance(manifest_heads, list) or not all(isinstance(item, dict) for item in manifest_heads):
        raise BundleError("manifest advertised_refs must be a list of objects")
    manifest_heads = sorted(manifest_heads, key=lambda item: str(item.get("ref", "")))
    actual_heads = _bundle_heads(bundle)
    if actual_heads != manifest_heads:
        raise BundleError(f"bundle advertised refs differ from manifest: {actual_heads!r} != {manifest_heads!r}")
    if {"sha": expected_sha, "ref": expected_ref} not in actual_heads:
        raise BundleError("bundle does not advertise requested main SHA")
    if any(item["ref"] == expected_ref and item["sha"] != expected_sha for item in actual_heads):
        raise BundleError("advertised main does not match requested SHA")

    bundle_verify_output = _verify_bundle_command(bundle)
    clone_dir = Path(args.clone_dir).resolve()
    if clone_dir.exists():
        if any(clone_dir.iterdir()):
            raise BundleError(f"clone destination is not empty: {clone_dir}")
        clone_dir.rmdir()
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--no-local", "--branch", "main", str(bundle), str(clone_dir)])
    cloned_head = _git(clone_dir, "rev-parse", "HEAD")
    if cloned_head != expected_sha:
        raise BundleError(f"cloned HEAD mismatch: expected {expected_sha}, got {cloned_head}")
    cloned_main = _git(clone_dir, "rev-parse", MAIN_REF)
    if cloned_main != expected_sha:
        raise BundleError(f"cloned main mismatch: expected {expected_sha}, got {cloned_main}")
    origin_main = _git(clone_dir, "rev-parse", "refs/remotes/origin/main")
    if origin_main != expected_sha:
        raise BundleError(f"cloned origin/main mismatch: expected {expected_sha}, got {origin_main}")
    canonical_remote = f"https://github.com/{args.expected_repository}.git"
    _git(clone_dir, "remote", "set-url", "origin", canonical_remote)
    if _git(clone_dir, "remote", "get-url", "origin") != canonical_remote:
        raise BundleError("failed to stamp canonical GitHub origin after verification")

    return {
        "repository": args.expected_repository,
        "repository_id": str(repository.get("id")),
        "source_sha": expected_sha,
        "source_ref": expected_ref,
        "artifact_name": metadata["artifact_name"],
        "bundle_sha256": bundle_sha256,
        "bundle_size_bytes": bundle.stat().st_size,
        "advertised_refs": actual_heads,
        "bundle_verify_output": bundle_verify_output,
        "cloned_head": cloned_head,
        "clone_dir": str(clone_dir),
        "origin": canonical_remote,
        "git_version": _run(["git", "--version"]).stdout.strip(),
    }


def _write_outputs(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    keys = (
        "artifact_name",
        "release_tag",
        "bundle_name",
        "manifest_name",
        "checksum_name",
        "bundle_sha256",
        "bundle_size_bytes",
        "source_sha",
        "source_ref",
    )
    with Path(path).open("a", encoding="utf-8") as handle:
        for key in keys:
            if key in payload:
                handle.write(f"{key}={payload[key]}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    authority = subparsers.add_parser("authority", help="verify an exact main SHA against origin")
    authority.add_argument("--repo-root", default=".")
    authority.add_argument("--source-sha", required=True)
    authority.add_argument("--source-ref", default=MAIN_REF)

    build = subparsers.add_parser("build", help="build manifest/checksum/Git bundle for an exact publication event")
    build.add_argument("--repo-root", default=".")
    build.add_argument("--output-dir", required=True)
    build.add_argument("--repository", required=True)
    build.add_argument("--repository-id", required=True)
    build.add_argument("--source-sha", required=True)
    build.add_argument("--source-ref", default=MAIN_REF)
    build.add_argument("--event-name", required=True)
    build.add_argument("--event-ref", required=True)
    build.add_argument("--event-sha", required=True)
    build.add_argument("--workflow", required=True)
    build.add_argument("--workflow-ref", required=True)
    build.add_argument("--workflow-sha", required=True)
    build.add_argument("--run-id", required=True)
    build.add_argument("--run-attempt", required=True)
    build.add_argument("--github-output")

    verify = subparsers.add_parser("verify", help="verify exact bundle identity and clone it")
    verify.add_argument("--bundle", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--checksum", required=True)
    verify.add_argument("--expected-repository", required=True)
    verify.add_argument("--expected-repository-id")
    verify.add_argument("--expected-sha", required=True)
    verify.add_argument("--expected-ref", default=MAIN_REF)
    verify.add_argument("--clone-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "authority":
            repo_root = Path(args.repo_root).resolve()
            verify_authority(repo_root, args.source_sha, args.source_ref)
            payload: dict[str, Any] = {
                "repository_remote": _git(repo_root, "remote", "get-url", "origin"),
                "source_sha": _require_sha(args.source_sha, "source SHA"),
                "source_ref": _require_main_ref(args.source_ref),
            }
        elif args.command == "build":
            payload = build_bundle(args)
            _write_outputs(args.github_output, payload)
        elif args.command == "verify":
            payload = verify_and_clone(args)
        else:  # pragma: no cover
            raise BundleError(f"unsupported command: {args.command}")
    except (BundleError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
