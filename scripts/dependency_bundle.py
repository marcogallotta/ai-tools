#!/usr/bin/env python3
"""Build, verify, install, and publish the canonical offline Python dependency bundle."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

SCHEMA_VERSION = 1
TARGET_PATH = Path("ci/dependency-bundle-target.json")
RELEASE_PREFIX = "dependency-bundle-"
BUNDLE_PREFIX = "ai-tools-python-deps-v1"


class BundleError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    else:
        text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return text.encode("utf-8")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read JSON {path}: {exc}") from exc


def _repo_root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def _load_target(repo_root: Path) -> dict[str, Any]:
    path = repo_root / TARGET_PATH
    target = _read_json(path)
    required = {
        "schema_version",
        "python_implementation",
        "python_version",
        "platform_system",
        "platform_architecture",
        "sysconfig_platform",
        "libc_name",
        "libc_version",
        "github_runner",
        "compatibility_manifests",
        "environments",
    }
    missing = sorted(required - set(target))
    if missing:
        raise BundleError(f"{TARGET_PATH} missing keys: {', '.join(missing)}")
    if target["schema_version"] != SCHEMA_VERSION:
        raise BundleError(
            f"unsupported target schema {target['schema_version']!r}; expected {SCHEMA_VERSION}"
        )
    if not isinstance(target["compatibility_manifests"], list) or not target["compatibility_manifests"]:
        raise BundleError("compatibility_manifests must be a non-empty list")
    if not isinstance(target["environments"], dict) or not target["environments"]:
        raise BundleError("environments must be a non-empty object")
    for name, spec in target["environments"].items():
        if not isinstance(spec, dict):
            raise BundleError(f"environment {name!r} must be an object")
        for key in ("requirements", "venv", "install_by_default"):
            if key not in spec:
                raise BundleError(f"environment {name!r} missing {key!r}")
    return target


def _manifest_hashes(repo_root: Path, target: dict[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in target["compatibility_manifests"]:
        if not isinstance(relative, str) or not relative:
            raise BundleError("compatibility manifest paths must be non-empty strings")
        path = (repo_root / relative).resolve()
        try:
            path.relative_to(repo_root)
        except ValueError as exc:
            raise BundleError(f"compatibility manifest escapes repository: {relative}") from exc
        if not path.is_file():
            raise BundleError(f"compatibility manifest is missing: {relative}")
        hashes[relative] = _sha256_file(path)
    return hashes


def _python_tag(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise BundleError(f"python_version must be MAJOR.MINOR.PATCH, got {version!r}")
    return f"cp{match.group(1)}{match.group(2)}"


def expected_metadata(repo_root: Path) -> dict[str, Any]:
    target = _load_target(repo_root)
    hashes = _manifest_hashes(repo_root, target)
    identity_payload = {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "dependency_manifest_sha256": hashes,
    }
    compatibility_sha256 = _sha256_bytes(_json_bytes(identity_payload))
    system = str(target["platform_system"]).lower()
    arch = str(target["platform_architecture"]).lower()
    bundle_id = (
        f"{BUNDLE_PREFIX}-{_python_tag(str(target['python_version']))}-"
        f"{system}-{arch}-{compatibility_sha256[:20]}"
    )
    archive_name = f"{bundle_id}.tar.gz"
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "release_tag": f"{RELEASE_PREFIX}{bundle_id}",
        "archive_name": archive_name,
        "checksum_name": f"{archive_name}.sha256",
        "manifest_name": f"{bundle_id}.manifest.json",
        "python_version": target["python_version"],
        "github_runner": target["github_runner"],
        "compatibility_sha256": compatibility_sha256,
        "dependency_manifest_sha256": hashes,
        "target": target,
    }


def _write_github_output(path: Path, metadata: dict[str, Any]) -> None:
    keys = (
        "bundle_id",
        "release_tag",
        "archive_name",
        "checksum_name",
        "manifest_name",
        "python_version",
        "github_runner",
        "compatibility_sha256",
    )
    with path.open("a", encoding="utf-8") as handle:
        for key in keys:
            value = metadata[key]
            if "\n" in str(value):
                raise BundleError(f"cannot write multiline GitHub output for {key}")
            handle.write(f"{key}={value}\n")


def _runtime_facts() -> dict[str, Any]:
    libc_name, libc_version = platform.libc_ver()
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_architecture": platform.machine(),
        "sysconfig_platform": sysconfig.get_platform(),
        "libc_name": libc_name,
        "libc_version": libc_version,
    }


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for token in value.split("."):
        match = re.match(r"\d+", token)
        if not match:
            break
        parts.append(int(match.group(0)))
    return tuple(parts)


def _verify_runtime(target: dict[str, Any], *, builder: dict[str, Any] | None = None) -> None:
    facts = _runtime_facts()
    runner = os.environ.get("AI_TOOLS_GITHUB_RUNNER")
    if runner and runner != target["github_runner"]:
        raise BundleError(
            "GitHub runner compatibility mismatch: "
            f"expected {target['github_runner']!r}, got {runner!r}"
        )
    checks = (
        ("python_implementation", target["python_implementation"]),
        ("python_version", target["python_version"]),
        ("platform_system", target["platform_system"]),
        ("platform_architecture", target["platform_architecture"]),
        ("sysconfig_platform", target["sysconfig_platform"]),
        ("libc_name", target["libc_name"]),
        ("libc_version", target["libc_version"]),
    )
    for key, expected in checks:
        if facts[key] != expected:
            raise BundleError(
                f"runtime compatibility mismatch for {key}: expected {expected!r}, got {facts[key]!r}"
            )
    if builder:
        required_libc = str(builder.get("libc_name", ""))
        required_version = str(builder.get("libc_version", ""))
        current_libc = str(facts.get("libc_name", ""))
        current_version = str(facts.get("libc_version", ""))
        if required_libc and current_libc != required_libc:
            raise BundleError(
                f"libc mismatch: bundle built with {required_libc!r}, runtime is {current_libc!r}"
            )
        if required_version and _version_tuple(current_version) < _version_tuple(required_version):
            raise BundleError(
                f"libc too old: bundle builder used {required_version}, runtime has {current_version}"
            )


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if completed.returncode != 0:
        detail = ""
        if capture:
            detail = f"\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        raise BundleError(f"command failed ({completed.returncode}): {' '.join(command)}{detail}")
    return completed


def _pip_version(python: Path | str) -> str:
    completed = _run([str(python), "-m", "pip", "--version"], capture=True)
    return completed.stdout.strip()


def _wheel_from_report_entry(entry: dict[str, Any], wheelhouse: Path) -> Path:
    download = entry.get("download_info") or {}
    url = str(download.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise BundleError(f"offline resolution escaped wheelhouse: {url or '<missing url>'}")
    path = Path(unquote(parsed.path)).resolve()
    try:
        path.relative_to(wheelhouse)
    except ValueError as exc:
        raise BundleError(f"resolved dependency is outside wheelhouse: {path}") from exc
    if path.suffix != ".whl" or not path.is_file():
        raise BundleError(f"bundle dependencies must be wheel files: {path}")
    return path


def _resolve_environment(
    repo_root: Path,
    wheelhouse: Path,
    name: str,
    spec: dict[str, Any],
    workspace: Path,
) -> tuple[list[dict[str, str]], set[Path]]:
    requirements = (repo_root / spec["requirements"]).resolve()
    if not requirements.is_file():
        raise BundleError(f"requirements for environment {name!r} are missing: {requirements}")
    venv = workspace / f"venv-{name}"
    _run([sys.executable, "-m", "venv", str(venv)])
    python = venv / "bin" / "python"
    report = workspace / f"report-{name}.json"
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--report",
            str(report),
            "-r",
            str(requirements),
        ],
        cwd=requirements.parent,
        capture=True,
    )
    _run([str(python), "-m", "pip", "check"], capture=True)
    payload = _read_json(report)
    locked: list[dict[str, str]] = []
    used: set[Path] = set()
    seen_names: set[str] = set()
    for entry in payload.get("install", []):
        metadata = entry.get("metadata") or {}
        project = str(metadata.get("name", "")).strip()
        version = str(metadata.get("version", "")).strip()
        if not project or not version:
            raise BundleError(f"pip report for {name!r} contains dependency without name/version")
        normalized = re.sub(r"[-_.]+", "-", project).lower()
        if normalized in seen_names:
            raise BundleError(f"duplicate resolved project in {name!r}: {project}")
        seen_names.add(normalized)
        wheel = _wheel_from_report_entry(entry, wheelhouse)
        used.add(wheel)
        locked.append(
            {
                "name": project,
                "version": version,
                "sha256": _sha256_file(wheel),
                "wheel": wheel.name,
            }
        )
    locked.sort(key=lambda item: re.sub(r"[-_.]+", "-", item["name"]).lower())
    return locked, used


def _lock_text(entries: Iterable[dict[str, str]]) -> str:
    return "".join(
        f"{entry['name']}=={entry['version']} --hash=sha256:{entry['sha256']}\n" for entry in entries
    )


def _add_tree_to_tar(tar: tarfile.TarFile, root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = tar.gettarinfo(str(path), arcname=relative)
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        if info.isdir():
            tar.addfile(info)
        elif info.isfile():
            with path.open("rb") as handle:
                tar.addfile(info, handle)
        else:
            raise BundleError(f"unsupported bundle filesystem entry: {path}")


def _write_deterministic_archive(bundle_root: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(mode="w", fileobj=zipped, format=tarfile.PAX_FORMAT) as tar:
                _add_tree_to_tar(tar, bundle_root)


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        destination_resolved = destination.resolve()
        for member in tar.getmembers():
            candidate = (destination / member.name).resolve()
            try:
                candidate.relative_to(destination_resolved)
            except ValueError as exc:
                raise BundleError(f"archive path escapes destination: {member.name}") from exc
            if member.issym() or member.islnk():
                raise BundleError(f"bundle may not contain links: {member.name}")
        tar.extractall(destination, filter="data")


def _verify_bundle_tree(repo_root: Path, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = expected_metadata(repo_root)
    manifest_path = root / "bundle-manifest.json"
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleError("bundle manifest schema mismatch")
    if manifest.get("bundle_id") != expected["bundle_id"]:
        raise BundleError(
            f"bundle identity mismatch: expected {expected['bundle_id']}, got {manifest.get('bundle_id')!r}"
        )
    if manifest.get("compatibility_sha256") != expected["compatibility_sha256"]:
        raise BundleError("bundle compatibility hash does not match checked-out dependency manifests")
    if manifest.get("dependency_manifest_sha256") != expected["dependency_manifest_sha256"]:
        raise BundleError("bundle dependency-manifest hashes do not match checkout")
    if manifest.get("target") != expected["target"]:
        raise BundleError("bundle target/platform manifest does not match checkout")
    _verify_runtime(expected["target"], builder=manifest.get("builder"))

    wheelhouse = root / "wheelhouse"
    if not wheelhouse.is_dir():
        raise BundleError("bundle wheelhouse directory is missing")
    declared = manifest.get("wheels")
    if not isinstance(declared, list) or not declared:
        raise BundleError("bundle manifest has no wheels")
    declared_names: set[str] = set()
    for entry in declared:
        relative = str(entry.get("path", ""))
        sha = str(entry.get("sha256", ""))
        path = root / relative
        if not relative.startswith("wheelhouse/") or not path.is_file():
            raise BundleError(f"declared wheel is missing or misplaced: {relative}")
        if path.suffix != ".whl":
            raise BundleError(f"declared dependency is not a wheel: {relative}")
        if _sha256_file(path) != sha:
            raise BundleError(f"wheel checksum mismatch: {relative}")
        declared_names.add(relative)
    actual_names = {f"wheelhouse/{path.name}" for path in wheelhouse.iterdir() if path.is_file()}
    if actual_names != declared_names:
        raise BundleError("bundle wheelhouse contents do not exactly match manifest")

    for name, spec in expected["target"]["environments"].items():
        lock_path = root / "resolved" / f"{name}.txt"
        if not lock_path.is_file():
            raise BundleError(f"resolved lock missing for environment {name!r}")
        declared_sha = (manifest.get("resolved_lock_sha256") or {}).get(name)
        if _sha256_file(lock_path) != declared_sha:
            raise BundleError(f"resolved lock checksum mismatch for environment {name!r}")
        if spec.get("requirements") not in expected["dependency_manifest_sha256"]:
            raise BundleError(
                f"environment {name!r} requirements are not a declared compatibility manifest"
            )
    return manifest, expected


def _verify_external_files(
    archive: Path, checksum: Path | None, manifest_asset: Path | None
) -> str:
    archive_sha = _sha256_file(archive)
    if checksum is not None:
        text = checksum.read_text(encoding="utf-8").strip()
        parts = text.split()
        if len(parts) < 2:
            raise BundleError(f"invalid checksum file: {checksum}")
        declared_sha = parts[0]
        declared_name = parts[-1].lstrip("*")
        if declared_name != archive.name:
            raise BundleError(
                f"checksum filename mismatch: expected {archive.name}, got {declared_name}"
            )
        if declared_sha != archive_sha:
            raise BundleError(
                f"archive checksum mismatch: expected {declared_sha}, got {archive_sha}"
            )
    if manifest_asset is not None:
        with tempfile.TemporaryDirectory(prefix="ai-tools-bundle-manifest-") as tmp:
            target = Path(tmp)
            _safe_extract(archive, target)
            internal = target / "bundle-manifest.json"
            if internal.read_bytes() != manifest_asset.read_bytes():
                raise BundleError("release manifest asset does not match archive manifest")
    return archive_sha


def cmd_expected(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    metadata = expected_metadata(repo_root)
    if args.github_output:
        _write_github_output(Path(args.github_output), metadata)
    if args.json:
        print(json.dumps(metadata, indent=2, sort_keys=True))
    elif not args.github_output:
        print(metadata["bundle_id"])
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    metadata = expected_metadata(repo_root)
    target = metadata["target"]
    _verify_runtime(target)
    wheelhouse = Path(args.wheelhouse).resolve()
    if not wheelhouse.is_dir():
        raise BundleError(f"wheelhouse does not exist: {wheelhouse}")
    unexpected = sorted(path.name for path in wheelhouse.iterdir() if path.is_file() and path.suffix != ".whl")
    if unexpected:
        raise BundleError(
            "canonical wheelhouse must contain wheels only; remove/build these entries first: "
            + ", ".join(unexpected)
        )

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / metadata["archive_name"]
    checksum = output_dir / metadata["checksum_name"]
    manifest_asset = output_dir / metadata["manifest_name"]
    for path in (archive, checksum, manifest_asset):
        if path.exists() and not args.force:
            raise BundleError(f"refusing to replace existing output: {path}")

    with tempfile.TemporaryDirectory(prefix="ai-tools-dependency-build-") as tmp:
        workspace = Path(tmp)
        resolved_dir = workspace / "bundle" / "resolved"
        bundle_wheels = workspace / "bundle" / "wheelhouse"
        resolved_dir.mkdir(parents=True)
        bundle_wheels.mkdir(parents=True)
        resolved: dict[str, list[dict[str, str]]] = {}
        used_wheels: set[Path] = set()
        for name, spec in target["environments"].items():
            entries, used = _resolve_environment(repo_root, wheelhouse, name, spec, workspace)
            resolved[name] = entries
            used_wheels.update(used)
            (resolved_dir / f"{name}.txt").write_text(_lock_text(entries), encoding="utf-8")

        if not used_wheels:
            raise BundleError("offline resolution selected no wheel files")
        # Preserve the complete canonical staging wheelhouse, not just the subset pip selected.
        # This keeps otherwise inaccessible/private wheels available in the immutable authority.
        staging_wheels = sorted(
            (path for path in wheelhouse.iterdir() if path.is_file() and path.suffix == ".whl"),
            key=lambda path: path.name.lower(),
        )
        if not staging_wheels:
            raise BundleError("canonical wheelhouse contains no wheel files")
        wheel_entries: list[dict[str, str]] = []
        for source in staging_wheels:
            sha = _sha256_file(source)
            shutil.copy2(source, bundle_wheels / source.name)
            wheel_entries.append({"path": f"wheelhouse/{source.name}", "sha256": sha})

        builder = _runtime_facts()
        builder["pip"] = _pip_version(sys.executable)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": metadata["bundle_id"],
            "compatibility_sha256": metadata["compatibility_sha256"],
            "dependency_manifest_sha256": metadata["dependency_manifest_sha256"],
            "target": target,
            "built_from_commit": args.source_commit,
            "builder": builder,
            "resolved_lock_sha256": {
                name: _sha256_file(resolved_dir / f"{name}.txt") for name in resolved
            },
            "resolved": resolved,
            "wheels": wheel_entries,
        }
        manifest_bytes = _json_bytes(manifest, pretty=True)
        (workspace / "bundle" / "bundle-manifest.json").write_bytes(manifest_bytes)
        _write_deterministic_archive(workspace / "bundle", archive)
        archive_sha = _sha256_file(archive)
        checksum.write_text(f"{archive_sha}  {archive.name}\n", encoding="utf-8")
        manifest_asset.write_bytes(manifest_bytes)

    print(json.dumps({
        "bundle_id": metadata["bundle_id"],
        "archive": str(archive),
        "sha256": archive_sha,
        "checksum": str(checksum),
        "manifest": str(manifest_asset),
    }, indent=2, sort_keys=True))
    return 0


def _install_one(
    repo_root: Path,
    extracted: Path,
    name: str,
    spec: dict[str, Any],
    evidence: Path | None,
) -> None:
    venv_path = (repo_root / spec["venv"]).resolve()
    try:
        venv_path.relative_to(repo_root)
    except ValueError as exc:
        raise BundleError(f"venv path escapes repository for {name!r}: {venv_path}") from exc
    if venv_path.exists():
        shutil.rmtree(venv_path)
    venv_path.parent.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "venv", str(venv_path)])
    python = venv_path / "bin" / "python"
    lock = extracted / "resolved" / f"{name}.txt"
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--find-links",
        str(extracted / "wheelhouse"),
        "--require-hashes",
        "-r",
        str(lock),
    ]
    completed = subprocess.run(command, cwd=repo_root, text=True, capture_output=True, check=False)
    if evidence:
        (evidence / f"pip-install-{name}.log").write_text(
            completed.stdout + completed.stderr, encoding="utf-8"
        )
    if completed.returncode != 0:
        raise BundleError(f"offline install failed for {name!r}; see evidence log")
    checked = subprocess.run(
        [str(python), "-m", "pip", "check"], cwd=repo_root, text=True, capture_output=True, check=False
    )
    if evidence:
        (evidence / f"pip-check-{name}.log").write_text(
            checked.stdout + checked.stderr, encoding="utf-8"
        )
    if checked.returncode != 0:
        raise BundleError(f"pip check failed for {name!r}; see evidence log")


def cmd_verify(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    archive = Path(args.archive).resolve()
    if not archive.is_file():
        raise BundleError(f"archive does not exist: {archive}")
    checksum = Path(args.checksum).resolve() if args.checksum else None
    manifest_asset = Path(args.manifest).resolve() if args.manifest else None
    archive_sha = _verify_external_files(archive, checksum, manifest_asset)
    with tempfile.TemporaryDirectory(prefix="ai-tools-dependency-verify-") as tmp:
        extracted = Path(tmp)
        _safe_extract(archive, extracted)
        manifest, expected = _verify_bundle_tree(repo_root, extracted)
    print(json.dumps({
        "bundle_id": expected["bundle_id"],
        "sha256": archive_sha,
        "built_from_commit": manifest.get("built_from_commit"),
    }, sort_keys=True))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    archive = Path(args.archive).resolve()
    if not archive.is_file():
        raise BundleError(f"archive does not exist: {archive}")
    checksum = Path(args.checksum).resolve() if args.checksum else None
    manifest_asset = Path(args.manifest).resolve() if args.manifest else None
    evidence = Path(args.evidence_dir).resolve() if args.evidence_dir else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "expected.json").write_bytes(_json_bytes(expected_metadata(repo_root), pretty=True))
    archive_sha = _verify_external_files(archive, checksum, manifest_asset)
    with tempfile.TemporaryDirectory(prefix="ai-tools-dependency-install-") as tmp:
        extracted = Path(tmp)
        _safe_extract(archive, extracted)
        manifest, expected = _verify_bundle_tree(repo_root, extracted)
        if evidence:
            (evidence / "bundle-manifest.json").write_bytes(
                _json_bytes(manifest, pretty=True)
            )
            (evidence / "archive.sha256").write_text(
                f"{archive_sha}  {archive.name}\n", encoding="utf-8"
            )
        for name, spec in expected["target"]["environments"].items():
            should_install = bool(spec["install_by_default"]) or (args.include_flake and name == "flake")
            if should_install:
                _install_one(repo_root, extracted, name, spec, evidence)
    print(f"installed {expected['bundle_id']} ({archive_sha})")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    repo_root = _repo_root(args.repo_root)
    metadata = expected_metadata(repo_root)
    archive = Path(args.archive).resolve()
    checksum = Path(args.checksum).resolve()
    manifest_asset = Path(args.manifest).resolve()
    for path in (archive, checksum, manifest_asset):
        if not path.is_file():
            raise BundleError(f"publication asset is missing: {path}")
    if archive.name != metadata["archive_name"]:
        raise BundleError(
            f"archive identity mismatch: expected {metadata['archive_name']}, got {archive.name}"
        )
    _verify_external_files(archive, checksum, manifest_asset)
    with tempfile.TemporaryDirectory(prefix="ai-tools-publish-verify-") as tmp:
        extracted = Path(tmp)
        _safe_extract(archive, extracted)
        manifest, _expected = _verify_bundle_tree(repo_root, extracted)
    if manifest.get("built_from_commit") != args.source_commit:
        raise BundleError(
            "source commit mismatch between publish command and bundle manifest: "
            f"{args.source_commit} != {manifest.get('built_from_commit')}"
        )
    if shutil.which("gh") is None:
        raise BundleError("GitHub CLI 'gh' is required for local Release publication")
    existing = subprocess.run(
        ["gh", "release", "view", metadata["release_tag"], "--repo", args.repo],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if existing.returncode == 0:
        raise BundleError(
            f"release {metadata['release_tag']} already exists; authoritative bundles are immutable"
        )
    notes = (
        "Canonical offline Python dependency bundle for ai-tools.\n\n"
        f"Bundle identity: `{metadata['bundle_id']}`\n"
        f"Compatibility hash: `{metadata['compatibility_sha256']}`\n"
        f"Built from commit: `{args.source_commit}`\n\n"
        "The matching GitHub Actions artifact is produced automatically by the "
        "dependency-bundle mirror workflow when this Release is published."
    )
    _run(
        [
            "gh",
            "release",
            "create",
            metadata["release_tag"],
            str(archive),
            str(checksum),
            str(manifest_asset),
            "--repo",
            args.repo,
            "--target",
            args.source_commit,
            "--title",
            metadata["bundle_id"],
            "--notes",
            notes,
        ]
    )
    print(f"published {metadata['release_tag']}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dependency_bundle.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    expected = subparsers.add_parser("expected", help="compute the bundle expected by this checkout")
    expected.add_argument("--repo-root")
    expected.add_argument("--json", action="store_true")
    expected.add_argument("--github-output")
    expected.set_defaults(func=cmd_expected)

    build = subparsers.add_parser("build", help="snapshot a prepared wheelhouse into an immutable bundle")
    build.add_argument("--repo-root")
    build.add_argument("--wheelhouse", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--source-commit", required=True)
    build.add_argument("--force", action="store_true")
    build.set_defaults(func=cmd_build)

    verify = subparsers.add_parser("verify", help="verify bundle identity, checksum, and compatibility")
    verify.add_argument("--repo-root")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--checksum")
    verify.add_argument("--manifest")
    verify.set_defaults(func=cmd_verify)

    install = subparsers.add_parser("install", help="recreate repository virtualenvs from a verified bundle")
    install.add_argument("--repo-root")
    install.add_argument("--archive", required=True)
    install.add_argument("--checksum")
    install.add_argument("--manifest")
    install.add_argument("--evidence-dir")
    install.add_argument("--include-flake", action="store_true")
    install.set_defaults(func=cmd_install)

    publish = subparsers.add_parser("publish", help="publish a verified local bundle as an immutable Release")
    publish.add_argument("--repo-root")
    publish.add_argument("--repo", default="marcogallotta/ai-tools")
    publish.add_argument("--archive", required=True)
    publish.add_argument("--checksum", required=True)
    publish.add_argument("--manifest", required=True)
    publish.add_argument("--source-commit", required=True)
    publish.set_defaults(func=cmd_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BundleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
