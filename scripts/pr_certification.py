#!/usr/bin/env python3
"""Prepare exact-head selector-driven PR certification from a formal Review event."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import io
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import integration_certification_plan as certification_plan  # noqa: E402
import pr_gate  # noqa: E402

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ADD_LANES_RE = re.compile(r"(?im)^\s*CERTIFICATION ADD LANES:\s*(.*?)\s*$")

NATIVE_WAIVER_RECORDS = (
    {
        "nodeid": "tests/postgresql/native/test_production_shaped_runtime.py::test_section4_service_database_disconnect_rolls_back_then_recovers_once",
        "expected_reason_sha256": "a73321063eef94cb68f134ff85b48a2a1eda77a2e3d60a5893a40dc8b288ac1b",
        "owner_task_gid": "1217428310522281",
        "review_by": "2026-09-07",
        "justification": "bare native certification lacks shared TEST Compose control; revisit before enabling external effects",
    },
    {
        "nodeid": "tests/postgresql/native/test_process_failure_command.py::test_command_process_disconnect_before_commit_fails_closed_and_recovers",
        "expected_reason_sha256": "b318bcda941f247dd3ca65b8444b0b19ab73e8b628f9d91a02917c7df0b69dc1",
        "owner_task_gid": "1217428310522281",
        "review_by": "2026-09-07",
        "justification": "covered by dish-pg-process-failure; bare native certification lacks Compose control",
    },
    {
        "nodeid": "tests/postgresql/native/test_process_failure_disconnect.py::test_projection_worker_fails_clearly_across_postgresql_disconnect",
        "expected_reason_sha256": "b318bcda941f247dd3ca65b8444b0b19ab73e8b628f9d91a02917c7df0b69dc1",
        "owner_task_gid": "1217428310522281",
        "review_by": "2026-09-07",
        "justification": "covered by dish-pg-process-failure; bare native certification lacks Compose control",
    },
    {
        "nodeid": "tests/postgresql/native/test_process_failure_disconnect.py::test_reconciliation_worker_writes_nothing_while_postgresql_is_down",
        "expected_reason_sha256": "b318bcda941f247dd3ca65b8444b0b19ab73e8b628f9d91a02917c7df0b69dc1",
        "owner_task_gid": "1217428310522281",
        "review_by": "2026-09-07",
        "justification": "covered by dish-pg-process-failure; bare native certification lacks Compose control",
    },
)
NATIVE_WAIVERS = tuple(
    json.dumps(record, sort_keys=True, separators=(",", ":")) for record in NATIVE_WAIVER_RECORDS
)


def _native_waivers_for_selection(*, mode: str, test_files: list[object]) -> tuple[str, ...]:
    if mode == "full":
        return NATIVE_WAIVERS
    selected_files = {str(test_file) for test_file in test_files}
    return tuple(
        json.dumps(record, sort_keys=True, separators=(",", ":"))
        for record in NATIVE_WAIVER_RECORDS
        if str(record["nodeid"]).split("::", 1)[0] in selected_files
    )


DISH_LANE_COMMANDS: dict[str, tuple[str, ...]] = {
    "smoke": (".venv/bin/python", "-m", "pytest", "--smoke"),
    "SQLite database-boundary": (".venv/bin/python", "-m", "pytest", "--database-boundary"),
    "PGlite primary": (
        ".venv/bin/python", "scripts/dish-pg-pglite", "--output", ".test-artifacts/pglite/report.json"
    ),
    "PGlite quarantine": (
        ".venv/bin/python", "scripts/dish-pg-pglite", "--output", ".test-artifacts/pglite/report.json"
    ),
    "default mutation sample": (".venv/bin/python", "-m", "tests.mutation_runner"),
    "Stage A mutation sample": (".venv/bin/python", "-m", "tests.mutation_runner", "--stage-a"),
    "source acceptance": (
        ".venv/bin/python", "scripts/dish-pg-acceptance", "--skip-full", "--output",
        ".test-artifacts/stage-a-acceptance/report.json",
    ),
    "flake diagnostics": (".venv-flake/bin/python", "-m", "tests.flake_runner", "rerun-detect"),
    "ordinary full suite": (".venv/bin/python", "-m", "pytest", "-q", "tests"),
}


class PRCertificationError(RuntimeError):
    pass


def _sha(value: object, *, label: str) -> str:
    result = str(value or "").strip().lower()
    if not SHA_RE.fullmatch(result):
        raise PRCertificationError(f"{label} must be an exact lowercase 40-hex commit SHA")
    return result


def review_additional_lanes(body: object) -> tuple[str, ...]:
    if not isinstance(body, str):
        return ()
    matches = ADD_LANES_RE.findall(body)
    if len(matches) > 1:
        raise PRCertificationError("formal Review contains multiple CERTIFICATION ADD LANES lines")
    if not matches:
        return ()
    value = matches[0].strip()
    if not value or value.upper() == "NONE":
        return ()
    lanes = tuple(sorted({part.strip() for part in re.split(r"[;,]", value) if part.strip()}))
    if not lanes:
        raise PRCertificationError("CERTIFICATION ADD LANES must name lanes or NONE")
    return lanes


def review_event_identity(event: dict[str, Any]) -> dict[str, object] | None:
    if event.get("action") != "submitted":
        return None
    review = event.get("review")
    pr = event.get("pull_request")
    if not isinstance(review, dict) or not isinstance(pr, dict):
        raise PRCertificationError("pull_request_review event is missing review/pull_request")
    if str(review.get("state", "")).lower() not in {"commented", "comment"}:
        return None
    if pr_gate.review_verdict(review.get("body")) != "MERGE":
        return None

    candidate = _sha(review.get("commit_id"), label="formal Review commit_id")
    head = pr.get("head")
    base = pr.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise PRCertificationError("pull_request_review event is missing head/base identity")
    current_head = _sha(head.get("sha"), label="event pull request head.sha")
    if current_head != candidate:
        raise PRCertificationError(
            f"formal Review is stale: commit_id {candidate} != current PR head {current_head}"
        )
    return {
        "candidate_sha": candidate,
        "base_sha": _sha(base.get("sha"), label="event pull request base.sha"),
        "pr_number": int(pr.get("number") or event.get("number") or 0),
        "review_id": int(review.get("id") or 0),
        "review_submitted_at": str(review.get("submitted_at") or ""),
        "semantic_additions": review_additional_lanes(review.get("body")),
    }


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo_root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise PRCertificationError(
            f"git {' '.join(shlex.quote(arg) for arg in args)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def exact_changed_paths(repo_root: Path, *, merge_base: str, candidate: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "diff", "--name-status", "-z", "--find-renames", merge_base, candidate],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise PRCertificationError(f"git diff failed: {completed.stderr.decode(errors='replace').strip()}")
    fields = completed.stdout.decode("utf-8").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            raise PRCertificationError("git diff emitted an empty status field")
        if status[0] in {"R", "C"}:
            if index + 1 >= len(fields):
                raise PRCertificationError("git diff emitted a truncated rename/copy record")
            paths.add(fields[index])
            paths.add(fields[index + 1])
            index += 2
        else:
            if index >= len(fields):
                raise PRCertificationError("git diff emitted a truncated path record")
            paths.add(fields[index])
            index += 1
    if not paths:
        raise PRCertificationError("formal Review candidate has no changed paths from merge base")
    return certification_plan.normalize_changed_paths(paths)


def _command(name: str, argv: Iterable[str], *, cwd: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {"name": name, "argv": list(argv)}
    if cwd:
        value["cwd"] = cwd
    return value


def _commands_for_target(target: dict[str, object], *, candidate: str) -> list[dict[str, object]]:
    runner = str(target["runner"])
    selector = str(target["selector"])
    target_id = str(target["id"])
    if runner == "repo-pytest":
        return [_command(target_id, ("dish/.venv/bin/python", "-m", "pytest", "-q", selector))]
    if runner == "tools-pytest":
        return [_command(target_id, ("tools/.venv/bin/python", "-m", "pytest", "-q", selector))]
    if runner == "dish-pytest":
        return [_command(target_id, (".venv/bin/python", "-m", "pytest", "-q", selector), cwd="dish")]
    if runner == "repo-python-full":
        return [
            _command(f"{target_id}:ci", ("dish/.venv/bin/python", "-m", "pytest", "-q", "ci/tests")),
            _command(f"{target_id}:tools", ("tools/.venv/bin/python", "-m", "pytest", "-q", "tools/tests")),
            _command(f"{target_id}:dish", (".venv/bin/python", "-m", "pytest", "-q", "tests"), cwd="dish"),
        ]
    if runner == "frontend-static":
        return [_command(target_id, ("npm", "--prefix", "dish/frontend", "run", "check:static"))]
    if runner == "browser":
        return [
            _command(f"{target_id}:harness", ("npm", "--prefix", "dish/frontend", "run", "test:browser")),
            _command(f"{target_id}:acceptance", ("npm", "--prefix", "dish/frontend", "run", "test:acceptance")),
        ]
    if runner == "pglite":
        return [_command(target_id, (".venv/bin/python", "scripts/dish-pg-pglite", "--output", ".test-artifacts/pglite/report.json"), cwd="dish")]
    if runner == "native-postgresql":
        argv = [
            "dish/.venv/bin/python", "dish/scripts/dish-pg-native-certification",
            "--output", ".test-artifacts/native-postgresql/report.json", "--expected-head", candidate,
        ]
        selected_files: list[object] = []
        mode = "full"
        if selector != "full":
            mode = "focused"
            selected_files = [selector]
            argv.extend(("--test-file", selector))
        for waiver in _native_waivers_for_selection(mode=mode, test_files=selected_files):
            argv.extend(("--waive-skip", waiver))
        return [_command(target_id, argv)]
    raise PRCertificationError(f"target {target_id} uses unsupported runner {runner}")


def build_execution_spec(plan: dict[str, object], *, plan_digest: str) -> dict[str, object]:
    identity = plan.get("identity")
    if not isinstance(identity, dict):
        raise PRCertificationError("planner output is missing identity")
    candidate = _sha(identity.get("candidate_sha"), label="planner candidate_sha")
    raw_targets = plan.get("selected_targets")
    if not isinstance(raw_targets, list):
        raise PRCertificationError("planner output is missing selected_targets")
    targets: list[dict[str, object]] = []
    for target in raw_targets:
        if not isinstance(target, dict):
            raise PRCertificationError("selected target must be an object")
        targets.append({
            "id": target["id"],
            "execution_boundary": target["execution_boundary"],
            "requirements": target["requirements"],
            "commands": _commands_for_target(target, candidate=candidate),
        })
    return {
        "schema": "dish-certification-execution-spec-v2",
        "candidate_sha": candidate,
        "plan_digest": plan_digest,
        "targets": targets,
    }


def _paths_present(repo_root: Path, revision: str, paths: Iterable[str]) -> tuple[str, ...]:
    present: list[str] = []
    for path in paths:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{revision}:{path}"], cwd=repo_root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if completed.returncode == 0:
            present.append(path)
    return tuple(present)


def _safe_extract_archive(payload: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive.getmembers():
            if member.name.startswith(("/", "\\")) or ".." in Path(member.name).parts:
                raise PRCertificationError("git archive contains an unsafe member path")
        archive.extractall(destination, filter="data")


def _base_graph_evidence(
    repo_root: Path, *, merge_base: str, paths: tuple[str, ...],
    candidate_envelope: dict[str, object],
) -> tuple[dict[str, object] | None, dict[str, object] | None, bool]:
    completed = subprocess.run(
        ["git", "archive", "--format=tar", merge_base], cwd=repo_root,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.returncode != 0:
        raise PRCertificationError(f"cannot materialize BASE graph inputs: {completed.stderr.decode(errors='replace').strip()}")
    with tempfile.TemporaryDirectory(prefix="dish-test-impact-base-") as temporary:
        base_root = Path(temporary)
        _safe_extract_archive(completed.stdout, base_root)
        engine = base_root / "scripts" / "test_impact_graph.py"
        if not engine.is_file():
            return None, None, False
        command = [sys.executable, str(engine), "obligations", "--provenance", "base"]
        for path in paths:
            command.extend(("--path", path))
        produced = subprocess.run(
            command, cwd=base_root, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        if produced.returncode != 0:
            return None, None, False
        try:
            base_envelope = json.loads(produced.stdout)
        except json.JSONDecodeError:
            return None, None, False
        base_arbiter = base_root / "scripts" / "test_impact_arbiter.py"
        if not base_arbiter.is_file():
            return base_envelope, None, False
        base_path = base_root / "base-envelope.json"
        candidate_path = base_root / "candidate-envelope.json"
        base_path.write_text(json.dumps(base_envelope), encoding="utf-8")
        candidate_path.write_text(json.dumps(candidate_envelope), encoding="utf-8")
        union = subprocess.run(
            [sys.executable, str(base_arbiter), "--base", str(base_path), "--candidate", str(candidate_path)],
            cwd=base_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if union.returncode != 0:
            return base_envelope, None, False
        try:
            base_arbiter_union = json.loads(union.stdout)
        except json.JSONDecodeError:
            return base_envelope, None, False
        if not isinstance(base_arbiter_union, dict):
            return base_envelope, None, False
        return base_envelope, base_arbiter_union, True


def _digest(plan: dict[str, object]) -> str:
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_output(path: Path | None, values: dict[str, str]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def prepare(
    *, event_path: Path, repo_root: Path, plan_path: Path, spec_path: Path,
    github_output: Path | None = None,
) -> dict[str, object]:
    event = json.loads(event_path.read_text(encoding="utf-8"))
    if not isinstance(event, dict):
        raise PRCertificationError("GitHub event payload must be an object")
    identity = review_event_identity(event)
    if identity is None:
        result = {"eligible": False}
        _write_output(github_output, {"eligible": "false"})
        return result

    candidate = str(identity["candidate_sha"])
    base = str(identity["base_sha"])
    checked_out = _sha(_git(repo_root, "rev-parse", "HEAD"), label="checked-out HEAD")
    if checked_out != candidate:
        raise PRCertificationError(
            f"workflow checkout is {checked_out}, expected formal Review commit_id {candidate}"
        )
    merge_base = _sha(_git(repo_root, "merge-base", base, candidate), label="merge base")
    paths = exact_changed_paths(repo_root, merge_base=merge_base, candidate=candidate)
    candidate_envelope = certification_plan.impact_graph.build_legacy_envelope(
        paths, provenance="candidate", repo_root=repo_root
    )
    base_envelope, base_arbiter_union, base_arbiter_compatible = _base_graph_evidence(
        repo_root, merge_base=merge_base, paths=paths, candidate_envelope=candidate_envelope
    )
    plan = certification_plan.build_repository_plan(
        paths,
        candidate_sha=candidate,
        base_sha=base,
        merge_base_sha=merge_base,
        semantic_additions=identity["semantic_additions"],
        semantic_review_complete=True,
        profile="PR_EXACT_HEAD",
        base_obligations=base_envelope,
        candidate_obligations=candidate_envelope,
        base_paths=_paths_present(repo_root, merge_base, paths),
        candidate_paths=_paths_present(repo_root, candidate, paths),
        arbiter_compatible=base_arbiter_compatible,
        base_arbiter_union=base_arbiter_union,
        repo_root=repo_root,
    )
    digest = _digest(plan)
    spec = build_execution_spec(plan, plan_digest=digest)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    outputs = {
        "eligible": "true",
        "candidate_sha": candidate,
        "base_sha": base,
        "merge_base_sha": merge_base,
        "plan_digest": digest,
        "all_boundary_fallback": "true" if plan["all_boundary_fallback"] else "false",
        "selected_groups": json.dumps(plan["selected_groups"], separators=(",", ":")),
    }
    _write_output(github_output, outputs)
    return {"eligible": True, "identity": identity, "plan": plan, "execution_spec": spec}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_certification.py")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--event-json", type=Path, required=True)
    prep.add_argument("--repo-root", type=Path, default=ROOT)
    prep.add_argument("--plan", type=Path, required=True)
    prep.add_argument("--execution-spec", type=Path, required=True)
    prep.add_argument("--github-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = prepare(
            event_path=args.event_json,
            repo_root=args.repo_root.resolve(),
            plan_path=args.plan,
            spec_path=args.execution_spec,
            github_output=args.github_output,
        )
    except (OSError, json.JSONDecodeError, PRCertificationError, certification_plan.CertificationPlanError) as exc:
        print(f"pr_certification: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
