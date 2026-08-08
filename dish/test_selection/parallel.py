"""Reviewed pytest-xdist-safe inventory, qualification, and command rendering."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .model import PolicyError

ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION_MANIFEST = Path("test_selection/parallel_safe_qualifications.json")
QUALIFICATION_SCHEMA_VERSION = 1
SHARED_QUALIFICATION_INPUTS = ("tests/conftest.py", "tests/support")
REQUIRED_WORKER_COUNTS = (2, 4, 8)
REQUIRED_PARALLEL_REPEATS = 3
QUALIFICATION_ENVIRONMENT_ID_FIELDS = (
    "python_executable",
    "python_version",
    "pytest_version",
    "xdist_version",
    "execnet_version",
    "requirements_test_sha256",
)


def _manifest_path(root: Path) -> Path:
    return root / QUALIFICATION_MANIFEST


def _load_manifest(root: Path | None = None) -> dict[str, Any]:
    root = root or ROOT
    path = _manifest_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot load parallel-safe qualification manifest {path}: {exc}") from exc
    if payload.get("schema_version") != QUALIFICATION_SCHEMA_VERSION:
        raise PolicyError(
            "unsupported parallel-safe qualification manifest schema: "
            f"{payload.get('schema_version')!r}"
        )
    files = payload.get("files")
    inventory = payload.get("inventory")
    if not isinstance(files, dict) or not files:
        raise PolicyError("parallel-safe qualification manifest has no reviewed files")
    if (
        not isinstance(inventory, list)
        or not all(isinstance(path, str) for path in inventory)
        or len(inventory) != len(set(inventory))
        or set(inventory) != set(files)
    ):
        raise PolicyError("parallel-safe qualification manifest inventory does not match file records")
    return payload


def _inventory(root: Path | None = None) -> tuple[str, ...]:
    return tuple(_load_manifest(root)["inventory"])


# Public compatibility name used by the lane/tests. The manifest is the single inventory authority.
PARALLEL_SAFE_TEST_FILES = _inventory()
_PARALLEL_SAFE_SET = frozenset(PARALLEL_SAFE_TEST_FILES)


def _normalize(test_files: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path.strip() for path in test_files if path.strip()))


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _shared_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for item in SHARED_QUALIFICATION_INPUTS:
        candidate = root / item
        if candidate.is_file():
            files.append(candidate)
            continue
        if not candidate.is_dir():
            continue
        files.extend(
            path
            for path in candidate.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    return tuple(sorted(files, key=lambda path: path.relative_to(root).as_posix()))


def parallel_safe_shared_sha256(root: Path | None = None) -> str:
    """Hash the shared fixture/helper scope that can change worker isolation semantics."""
    root = root or ROOT
    digest = hashlib.sha256()
    for path in _shared_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_parallel_safe_files(test_files: Iterable[str]) -> tuple[str, ...]:
    """Validate inventory membership only; serial diagnosis stays available after drift."""
    selected = _normalize(test_files)
    if not selected:
        return PARALLEL_SAFE_TEST_FILES
    unsupported = sorted(set(selected) - _PARALLEL_SAFE_SET)
    if unsupported:
        raise PolicyError("parallel-safe execution is not reviewed for: " + ", ".join(unsupported))
    return selected


def _successful_result(result: object) -> bool:
    return (
        isinstance(result, Mapping)
        and result.get("returncode") == 0
        and result.get("failed", 0) == 0
        and result.get("errors", 0) == 0
        and isinstance(result.get("passed"), int)
        and result.get("passed", 0) > 0
    )


def qualification_environment_id(environment: Mapping[str, Any]) -> str:
    """Hash the execution-environment facts that must stay fixed across qualification phases."""
    payload = {field: environment.get(field) for field in QUALIFICATION_ENVIRONMENT_ID_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def qualification_run_problem(
    result: object,
    *,
    exercise_files: Sequence[str],
    workers: int | None,
    repeat: int,
    require_participation: bool,
) -> str | None:
    """Validate one recorded qualification run, including file participation when required."""
    if not isinstance(result, Mapping):
        return "qualification run result is missing or malformed"
    if result.get("workers") != workers:
        return f"qualification run records workers={result.get('workers')!r}; expected {workers!r}"
    if result.get("repeat") != repeat:
        return f"qualification run records repeat={result.get('repeat')!r}; expected {repeat}"
    if not _successful_result(result):
        return "qualification run did not complete successfully"
    if not require_participation:
        return None
    file_results = result.get("file_results")
    if not isinstance(file_results, Mapping):
        return "qualification run does not record per-file test participation"
    for exercise_file in exercise_files:
        counts = file_results.get(exercise_file)
        if not isinstance(counts, Mapping):
            return f"qualification run does not prove participation for {exercise_file}"
        tests = counts.get("tests")
        passed = counts.get("passed")
        if (
            not isinstance(tests, int)
            or tests <= 0
            or not isinstance(passed, int)
            or passed <= 0
        ):
            return f"qualification run does not prove executed passing tests for {exercise_file}"
    return None


def _validate_evidence(evidence: object, *, path: str, reviewed_sha256: str, shared_sha256: str) -> str | None:
    if not isinstance(evidence, Mapping):
        return "qualification evidence is missing or malformed"
    kind = evidence.get("kind")
    if kind not in {"batch", "file"}:
        return "qualification evidence kind is missing or unsupported"
    evidence_id = evidence.get("evidence_id")
    if not isinstance(evidence_id, str) or qualification_evidence_id(evidence) != evidence_id:
        return "qualification evidence content does not match its evidence ID"
    exercise_files: Sequence[str]
    require_participation = kind == "file"
    if kind == "file":
        exercise_files = evidence.get("exercise_files")
        witness_sha256 = evidence.get("witness_sha256")
        if (
            not isinstance(exercise_files, list)
            or len(exercise_files) != max(REQUIRED_WORKER_COUNTS)
            or len(set(exercise_files)) != max(REQUIRED_WORKER_COUNTS)
            or exercise_files[0] != path
            or not isinstance(witness_sha256, Mapping)
            or len(witness_sha256) != max(REQUIRED_WORKER_COUNTS) - 1
            or set(witness_sha256) != set(exercise_files[1:])
        ):
            return "per-file qualification evidence lacks the required concurrency witness set"
        environment = evidence.get("environment")
        environment_identity = evidence.get("environment_identity")
        if (
            not isinstance(environment, Mapping)
            or not isinstance(environment_identity, str)
            or qualification_environment_id(environment) != environment_identity
        ):
            return "per-file qualification evidence environment identity is missing or inconsistent"
    else:
        exercise_files = ()
    files = evidence.get("files")
    hashes = evidence.get("file_sha256")
    if not isinstance(files, list) or path not in files:
        return "qualification evidence does not cover this file"
    if not isinstance(hashes, Mapping) or hashes.get(path) != reviewed_sha256:
        return "qualification evidence does not match the reviewed file identity"
    if evidence.get("shared_sha256") != shared_sha256:
        return "qualification evidence does not match the reviewed shared fixture/helper identity"
    static_review = evidence.get("static_review")
    if not isinstance(static_review, Mapping) or static_review.get("findings") != []:
        return "qualification evidence does not contain a clean static isolation review"
    serial = evidence.get("serial")
    if not isinstance(serial, list) or len(serial) != 1:
        return "qualification evidence does not contain a successful serial baseline"
    serial_problem = qualification_run_problem(
        serial[0],
        exercise_files=exercise_files,
        workers=None,
        repeat=1,
        require_participation=require_participation,
    )
    if serial_problem:
        return "qualification serial evidence is invalid: " + serial_problem
    parallel = evidence.get("parallel")
    if not isinstance(parallel, Mapping):
        return "qualification evidence does not contain repeated xdist runs"
    for workers in REQUIRED_WORKER_COUNTS:
        runs = parallel.get(str(workers))
        if not isinstance(runs, list) or len(runs) != REQUIRED_PARALLEL_REPEATS:
            return (
                f"qualification evidence requires {REQUIRED_PARALLEL_REPEATS} successful "
                f"-n {workers} runs"
            )
        repeats = {run.get("repeat") for run in runs if isinstance(run, Mapping)}
        if repeats != set(range(1, REQUIRED_PARALLEL_REPEATS + 1)):
            return (
                f"qualification evidence requires distinct -n {workers} repetitions "
                f"1..{REQUIRED_PARALLEL_REPEATS}"
            )
        for run in runs:
            repeat = run.get("repeat") if isinstance(run, Mapping) else None
            run_problem = qualification_run_problem(
                run,
                exercise_files=exercise_files,
                workers=workers,
                repeat=repeat if isinstance(repeat, int) else -1,
                require_participation=require_participation,
            )
            if run_problem:
                return f"qualification -n {workers} evidence is invalid: {run_problem}"
    return None


def _active_evidence(manifest: Mapping[str, Any], path: str, record: Mapping[str, Any]) -> object:
    active = record.get("active_evidence")
    if not isinstance(active, Mapping):
        return None
    kind = active.get("kind")
    evidence_id = active.get("id")
    if not isinstance(evidence_id, str):
        return None
    if kind == "batch":
        batch = manifest.get("batch_evidence")
        return batch.get(evidence_id) if isinstance(batch, Mapping) else None
    if kind == "file":
        history = record.get("history")
        if not isinstance(history, list):
            return None
        for evidence in reversed(history):
            if isinstance(evidence, Mapping) and evidence.get("evidence_id") == evidence_id:
                return evidence
    return None


def parallel_safe_qualification_reason(path: str, *, root: Path | None = None) -> str | None:
    """Return the fail-closed reason a reviewed file is not currently parallel-qualified."""
    root = root or ROOT
    manifest = _load_manifest(root)
    record = manifest["files"].get(path)
    if not isinstance(record, Mapping):
        return "not in the reviewed parallel-safe inventory"
    candidate = root / path
    if not candidate.is_file():
        return "reviewed file is missing; requires explicit requalification"
    reviewed_sha256 = record.get("reviewed_sha256")
    shared_sha256 = record.get("shared_sha256")
    if not isinstance(reviewed_sha256, str) or not isinstance(shared_sha256, str):
        return "qualification record is malformed; requires explicit requalification"
    evidence_problem = _validate_evidence(
        _active_evidence(manifest, path, record),
        path=path,
        reviewed_sha256=reviewed_sha256,
        shared_sha256=shared_sha256,
    )
    if evidence_problem:
        return evidence_problem
    if _hash_file(candidate) != reviewed_sha256:
        return "changed since parallel review; requires explicit requalification"
    if parallel_safe_shared_sha256(root) != shared_sha256:
        return "shared fixtures/helpers changed since parallel review; requires explicit requalification"
    return None


def parallel_safe_qualification_drift(test_files: Iterable[str]) -> tuple[str, ...]:
    """Return reviewed files whose current file/shared content is no longer qualified."""
    selected = _normalize(test_files)
    return tuple(path for path in selected if parallel_safe_qualification_reason(path) is not None)


def parallel_safe_partition(
    test_files: Iterable[str], *, root: Path | None = None
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Partition reviewed files into worker-qualified and serial-fallback groups."""
    selected = validate_parallel_safe_files(test_files)
    qualified: list[str] = []
    blocked: list[tuple[str, str]] = []
    for path in selected:
        reason = parallel_safe_qualification_reason(path, root=root)
        if reason is None:
            qualified.append(path)
        else:
            blocked.append((path, reason))
    return tuple(qualified), tuple(blocked)


def parallel_safe_blockers(test_files: Iterable[str]) -> tuple[str, ...]:
    """Explain why a focused selection cannot use supported parallel execution."""
    selected = _normalize(test_files)
    if not selected:
        return ("no reviewed focused tests",)
    unsupported = tuple(sorted(set(selected) - _PARALLEL_SAFE_SET))
    reviewed = tuple(path for path in selected if path in _PARALLEL_SAFE_SET)
    blocked: list[str] = list(unsupported)
    for path in reviewed:
        reason = parallel_safe_qualification_reason(path)
        if reason is not None:
            blocked.append(f"{path} ({reason})")
    return tuple(blocked)


def parallel_safe_eligible(test_files: Iterable[str]) -> bool:
    """Return whether a non-empty focused set is reviewed and currently qualified."""
    selected = _normalize(test_files)
    return bool(selected) and not parallel_safe_blockers(selected)


def require_parallel_safe_qualification(test_files: Iterable[str]) -> tuple[str, ...]:
    """Fail closed if an explicitly selected reviewed file is no longer qualified."""
    selected = validate_parallel_safe_files(test_files)
    blockers = parallel_safe_blockers(selected)
    if blockers:
        raise PolicyError(
            "parallel-safe qualification drift: "
            + "; ".join(blockers)
            + ". Run the serial selection and scripts/dish-parallel-safe-qualify for each changed "
            "file; do not hand-edit qualification identities."
        )
    return selected



def parallel_safe_witness_files(
    target: str,
    *,
    count: int = 7,
    root: Path | None = None,
) -> tuple[str, ...]:
    """Choose unchanged previously reviewed files as concurrency witnesses for requalification.

    Current shared-scope qualification is intentionally ignored: this must still work after a
    shared fixture/helper change invalidates every active qualification at once. Witness file
    content and its prior evidence must still match, so a drifted test cannot silently become a
    witness.
    """
    root = root or ROOT
    manifest = _load_manifest(root)
    witnesses: list[str] = []
    for path, record in manifest["files"].items():
        if path == target or not isinstance(record, Mapping):
            continue
        candidate = root / path
        reviewed_sha256 = record.get("reviewed_sha256")
        shared_sha256 = record.get("shared_sha256")
        if (
            not candidate.is_file()
            or not isinstance(reviewed_sha256, str)
            or not isinstance(shared_sha256, str)
            or _hash_file(candidate) != reviewed_sha256
        ):
            continue
        if _validate_evidence(
            _active_evidence(manifest, path, record),
            path=path,
            reviewed_sha256=reviewed_sha256,
            shared_sha256=shared_sha256,
        ) is not None:
            continue
        witnesses.append(path)
        if len(witnesses) == count:
            return tuple(witnesses)
    raise PolicyError(
        f"parallel-safe requalification needs {count} unchanged reviewed witness files; "
        f"only {len(witnesses)} are available"
    )

def parallel_safe_command(test_files: Iterable[str], *, workers: int) -> str:
    """Render the governed opt-in command for one qualified focused selection."""
    if workers < 1:
        raise PolicyError("parallel worker count must be at least 1")
    selected = require_parallel_safe_qualification(test_files)
    parts = [
        ".venv/bin/python",
        "scripts/dish-test-lane",
        "parallel-safe",
        "--workers",
        str(workers),
    ]
    for path in selected:
        parts.extend(("--test-file", path))
    return " ".join(shlex.quote(part) for part in parts)


def qualification_evidence_id(evidence: Mapping[str, Any]) -> str:
    """Return a content-addressed identifier for immutable qualification evidence."""
    payload = dict(evidence)
    payload.pop("evidence_id", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def record_parallel_safe_qualification(
    path: str,
    evidence: Mapping[str, Any],
    *,
    root: Path | None = None,
) -> str:
    """Atomically activate one completed per-file qualification evidence record."""
    root = root or ROOT
    manifest = _load_manifest(root)
    record = manifest["files"].get(path)
    if not isinstance(record, dict):
        raise PolicyError(f"cannot requalify unreviewed parallel-safe file: {path}")
    evidence_payload = dict(evidence)
    evidence_id = qualification_evidence_id(evidence_payload)
    evidence_payload["evidence_id"] = evidence_id
    file_sha256 = evidence_payload.get("file_sha256")
    if not isinstance(file_sha256, Mapping) or not isinstance(file_sha256.get(path), str):
        raise PolicyError("qualification evidence is missing the selected file identity")
    shared_sha256 = evidence_payload.get("shared_sha256")
    if not isinstance(shared_sha256, str):
        raise PolicyError("qualification evidence is missing the shared fixture/helper identity")
    problem = _validate_evidence(
        evidence_payload,
        path=path,
        reviewed_sha256=file_sha256[path],
        shared_sha256=shared_sha256,
    )
    if problem:
        raise PolicyError("refusing incomplete parallel-safe qualification evidence: " + problem)

    history = record.setdefault("history", [])
    if not isinstance(history, list):
        raise PolicyError(f"parallel-safe qualification history is malformed for {path}")
    history.append(evidence_payload)
    record["reviewed_sha256"] = file_sha256[path]
    record["shared_sha256"] = shared_sha256
    record["active_evidence"] = {"kind": "file", "id": evidence_id}

    manifest_path = _manifest_path(root)
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=manifest_path.name + ".",
        dir=manifest_path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return evidence_id
