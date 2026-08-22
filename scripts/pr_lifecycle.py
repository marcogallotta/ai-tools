#!/usr/bin/env python3
"""Durable PR lifecycle status and dispatch for Dish."""
from __future__ import annotations

import sys
import io
import json
from pathlib import Path
import zipfile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from pr_lifecycle_helpers import _parse_time, _utcnow
from pr_lifecycle_external_replay import replay_external_dependency
from pr_lifecycle_engine_inspect import LifecycleInspectMixin
from pr_lifecycle_engine_actions import LifecycleActionsMixin
from pr_lifecycle_workstream import WorkstreamLifecycleMixin, current_review_state
from pr_lifecycle_authoring_actions import LifecycleAuthoringActionsMixin
from pr_lifecycle_publication_completion import FRESH_AUTHORING_REQUIRED, classify_publication_route, classify_receiver_bundle, render_publication_fallback_notice
from pr_lifecycle_integration_certification import LocalIntegrationCertificationMixin
from pr_lifecycle_local_integration import LocalIntegrationLauncher, checkpoint_claim
from pr_lifecycle_terminal import TerminalCleanupDispatcher
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_projection import atomic_write, build_projection, read_projection
from pr_lifecycle_task_state import execution_truth, ensure_projection_comment
from pr_lifecycle_rollout import reconstruct as reconstruct_rollout, rollout_projection
import pr_lifecycle_controller
from pr_certification import SELECTOR_GAP_OWNER_TASKS, selector_gap_owner_operations

OBSERVATION_PROJECTS_ENV = "DISH_PR_LIFECYCLE_PROJECT_GIDS"


def _sync_selector_gap_owner_surfaces(engine: "LifecycleEngine") -> list[dict[str, object]]:
    if engine.asana is None:
        return []
    comment_reader = getattr(engine.github, "get_repository_comments", None)
    if not callable(comment_reader):
        return []
    comments = comment_reader()
    stories = {
        task_gid: engine.asana.get_stories(task_gid)
        for _, task_gid in SELECTOR_GAP_OWNER_TASKS
    }
    operations = selector_gap_owner_operations(comments, stories)
    for operation in operations:
        task_gid = str(operation["task_gid"])
        marker = str(operation["marker"])
        engine.asana.add_comment(task_gid, str(operation["body"]))
        if not any(marker in str(story.get("text") or story.get("body") or "")
                   for story in engine.asana.get_stories(task_gid)):
            raise LifecycleError(
                f"selector-gap owner update was not observed on Asana task {task_gid}"
            )
    return operations


def _configured_observation_projects() -> list[str]:
    return [
        value.strip()
        for value in str(os.getenv(OBSERVATION_PROJECTS_ENV) or "").split(",")
        if value.strip()
    ]


class LifecycleEngine(
    LocalIntegrationCertificationMixin,
    WorkstreamLifecycleMixin,
    LifecycleInspectMixin,
    LifecycleAuthoringActionsMixin,
    LifecycleActionsMixin,
):
    def dispatch(
        self,
        *,
        include_closed=False,
        workspace=None,
        local_reviewer=None,
        implementation_fixer=None,
        terminal_cleaner=None,
        notify=None,
    ):
        """Release reviewed stacks unless an intermediate merge still needs target recovery."""
        values = super().dispatch(
            include_closed=include_closed,
            workspace=workspace,
            local_reviewer=local_reviewer,
            implementation_fixer=implementation_fixer,
            terminal_cleaner=terminal_cleaner,
            notify=notify,
        )
        try:
            _sync_selector_gap_owner_surfaces(self)
        except LifecycleError as exc:
            (notify or (lambda _: None))(f"Selector-gap owner sync unavailable: {exc}")
        candidates = self._workstream_candidates(values)
        releasable: set[int] = set()
        for candidate in candidates.values():
            if (
                candidate.complete
                and not candidate.source_complete
                and current_review_state(candidate, self.github).status == "merge"
                and not any(member.publication_state == "merged" for member in candidate.members)
            ):
                releasable.update(member.pr_number for member in candidate.members)
        if not releasable:
            return values

        by_number = {value.number: value for value in values}
        for number in sorted(releasable & by_number.keys()):
            current = self.inspect(self.github.get_pr(number))
            by_number[number] = self.dispatch_one(
                current,
                workspace=workspace,
                local_reviewer=local_reviewer,
                implementation_fixer=implementation_fixer,
                terminal_cleaner=terminal_cleaner,
                notify=notify or (lambda _: None),
            )
        return [by_number[value.number] for value in values]

    def _external_resolution_boundary(self, lifecycle):
        try:
            active, resolution = replay_external_dependency(
                self.github.get_comments(lifecycle.number)
            )
        except LifecycleError:
            return None
        check = pr_gate.REQUIRED_ORDINARY_CI_CONTEXT
        if resolution is not None and resolution.check == check:
            return resolution.timestamp, "external dependency was explicitly resolved"
        if active is None or active.check != check:
            return None
        if active.owner_pr is not None:
            try:
                owner = self.github.get_pr(active.owner_pr)
            except (LifecycleError, AssertionError):
                owner = None
            if owner is not None and bool(owner.get("merged") or owner.get("merged_at")):
                return (
                    _parse_time(owner.get("merged_at")),
                    f"external owner PR #{active.owner_pr} is merged",
                )
            return None
        if self.asana is None:
            return None
        try:
            owner_task = self.asana.get_task(active.task_gid)
        except LifecycleError:
            owner_task = None
        if owner_task is not None and bool(owner_task.get("completed")):
            return (
                _parse_time(owner_task.get("completed_at")),
                f"external owner task {active.task_gid} is complete",
            )
        return None

    def inspect(self, pr):
        lifecycle = super().inspect(pr)
        if lifecycle.human_action is not None:
            if lifecycle.state == LifecycleState.LOCAL_IMPLEMENTATION_REQUIRED:
                lifecycle.human_action = (
                    f"give PR #{lifecycle.number} to a local Implementation agent; full handoff is on the PR"
                )
            elif lifecycle.state == LifecycleState.LOCAL_CERTIFICATION_REQUIRED:
                lifecycle.human_action = (
                    f"give PR #{lifecycle.number} to a local Integration agent for exact-head certification; "
                    "full handoff is on the PR"
                )
        if (
            not lifecycle.gate
            or lifecycle.gate.get("diagnosis")
            != pr_gate.GateDiagnosis.FAILED_REQUIRED_CI.value
        ):
            return lifecycle
        boundary = self._external_resolution_boundary(lifecycle)
        if boundary is None:
            return lifecycle
        resolved_at, reason = boundary
        run_started_at = _parse_time(
            lifecycle.gate.get("required_workflow_run_started_at")
        )
        if (
            resolved_at is not None
            and run_started_at is not None
            and run_started_at > resolved_at
        ):
            return lifecycle
        stale = dict(lifecycle.gate)
        stale["diagnosis"] = pr_gate.GateDiagnosis.EVIDENCE_MISSING_OR_STALE.value
        stale["reason"] = (
            f"{reason}; stale failed CI. Integration must refresh/re-run exact-head "
            "evidence before assigning failure ownership."
        )
        lifecycle.state = LifecycleState.REVIEW_PASSED
        lifecycle.state_label = STATE_LABELS[LifecycleState.REVIEW_PASSED]
        lifecycle.gate = stale
        lifecycle.residual_reason = (
            "Integration evidence is stale after external dependency resolution; "
            "refresh/re-run exact-head evidence."
        )
        lifecycle.human_action = None
        return lifecycle

def _build_engine(
    args: argparse.Namespace,
) -> tuple[
    LifecycleEngine, WorkspaceAgentDispatcher | None, LocalReviewDispatcher,
    ImplementationFixRouter, TerminalCleanupDispatcher,
]:
    token = args.github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise LifecycleError("GitHub token is required via --github-token, GITHUB_TOKEN, or GH_TOKEN")
    http = JSONHTTPClient(timeout=args.http_timeout)
    github = GitHubREST(args.repo, token, api_root=args.github_api_root, http=http)
    asana_token = args.asana_token or os.getenv("ASANA_ACCESS_TOKEN")
    asana = AsanaREST(asana_token, http=http) if asana_token else None
    authority = args.integration_authority or os.getenv("DISH_INTEGRATION_AUTHORITY") == "bounded-reviewed-head"
    local_integration_command = (
        args.local_integration_launcher or os.getenv("DISH_LOCAL_INTEGRATION_COMMAND")
    )
    integration_capable = bool(local_integration_command) and not args.no_integration_capability
    engine = LifecycleEngine(
        github,
        asana=asana,
        integration_authority=authority,
        integration_capable=integration_capable,
        merge_method=args.merge_method,
    )
    workspace_token = args.workspace_token or os.getenv("DISH_WORKSPACE_AGENT_ACCESS_TOKEN")
    review_trigger = args.review_trigger_id or os.getenv("DISH_REVIEW_API_TRIGGER_ID")
    worker_trigger = args.worker_trigger_id or os.getenv("DISH_WORKER_API_TRIGGER_ID")
    workspace = None
    if workspace_token or review_trigger or worker_trigger:
        workspace = WorkspaceAgentDispatcher(
            access_token=workspace_token or "",
            review_trigger_id=review_trigger,
            worker_trigger_id=worker_trigger,
            api_root=args.workspace_api_root,
        )
    local = LocalReviewDispatcher(args.local_reviewer or os.getenv("DISH_LOCAL_REVIEW_COMMAND"))
    legacy_fix_command = args.implementation_fixer or os.getenv("DISH_IMPLEMENTATION_FIX_COMMAND")
    legacy_fix_host = str(os.getenv("DISH_IMPLEMENTATION_FIX_HOST") or "").strip().lower()
    chatgpt_fix_command = os.getenv("DISH_CHATGPT_IMPLEMENTATION_FIX_COMMAND")
    local_fix_command = os.getenv("DISH_LOCAL_IMPLEMENTATION_FIX_COMMAND")
    legacy_error = None
    if legacy_fix_command:
        if legacy_fix_host == "chatgpt":
            chatgpt_fix_command = chatgpt_fix_command or legacy_fix_command
        elif legacy_fix_host == "local":
            local_fix_command = local_fix_command or legacy_fix_command
        elif not chatgpt_fix_command and not local_fix_command:
            legacy_error = (
                "legacy DISH_IMPLEMENTATION_FIX_COMMAND/--implementation-fixer is unclassified; "
                "set DISH_IMPLEMENTATION_FIX_HOST=chatgpt|local or configure the host-specific command"
            )
    fixer = ImplementationFixRouter(
        chatgpt_command=chatgpt_fix_command,
        local_command=local_fix_command,
        legacy_error=legacy_error,
    )
    engine.local_integration_launcher = LocalIntegrationLauncher(local_integration_command)
    terminal_command = args.terminal_cleaner or os.getenv("DISH_TERMINAL_CLEANUP_COMMAND")
    if terminal_command is None:
        terminal_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT_DIR.parent / 'tools' / 'agent-worktree'))}"
    terminal_cleaner = TerminalCleanupDispatcher(terminal_command, repo_path=str(SCRIPT_DIR.parent))
    return engine, workspace, local, fixer, terminal_cleaner


def _render_json(values: list[PRLifecycle], *, repository: str) -> str:
    return json.dumps(
        {
            "schema": "dish-pr-lifecycle-status-v1",
            "repository": repository,
            "generated_at": _utcnow().isoformat(),
            "pull_requests": [value.json() for value in values],
        },
        indent=2,
        sort_keys=True,
    )


def _truncate(value: str, width: int) -> str:
    value = value.replace("\n", " ").strip()
    if len(value) <= width:
        return value
    return value[: max(1, width - 1)] + "…"


def _render_table(values: list[PRLifecycle]) -> str:
    headers = ["PR", "STATE", "HEAD", "REVIEW", "TASK", "RESIDUAL / ACTION"]
    rows: list[list[str]] = []
    for value in values:
        task = ",".join(value.task_ids) if value.task_ids else "-"
        review = value.review_verdict or value.review_class or "-"
        residual = action_first_status(value)
        rows.append(
            [
                f"#{value.number}",
                value.state_label,
                value.head[:10],
                review,
                task,
                residual,
            ]
        )
    widths = [4, 41, 10, 16, 16, 96]
    lines = ["  ".join(_truncate(header, width).ljust(width) for header, width in zip(headers, widths))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(_truncate(cell, width).ljust(width) for cell, width in zip(row, widths)))
    return "\n".join(lines)


def _notification_printer(message: str) -> None:
    print(message, file=sys.stderr)


def _task_observation_cycle(
    engine: LifecycleEngine,
    values: list[PRLifecycle],
    *,
    configured_projects: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if engine.asana is None:
        return [], {
            "status": "UNKNOWN",
            "projects": [],
            "reason": "Asana is not configured",
        }
    project_ids: set[str] = set()
    for configured in configured_projects:
        gid = str(configured).strip()
        if not TASK_GID_RE.fullmatch(gid):
            raise LifecycleError(f"invalid configured Asana observation project GID: {gid!r}")
        project_ids.add(gid)
    for value in values:
        for task in value.asana:
            for membership in task.get("memberships") or []:
                if isinstance(membership, Mapping) and isinstance(membership.get("project"), Mapping):
                    gid = str(membership["project"].get("gid") or "")
                    if gid:
                        project_ids.add(gid)
    if not project_ids:
        return [], {
            "status": "UNKNOWN",
            "projects": [],
            "reason": "no Asana project scope could be established from linked task memberships",
        }

    tasks: dict[str, dict[str, Any]] = {}
    scope_errors: list[dict[str, str]] = []
    for project_gid in sorted(project_ids):
        try:
            listed = engine.asana.list_project_tasks(project_gid)
        except LifecycleError as exc:
            scope_errors.append({"project": project_gid, "error": str(exc)})
            continue
        for task in listed:
            gid = str(task.get("gid") or "")
            if not gid or gid in tasks:
                continue
            try:
                authoritative = engine.asana.get_task(gid)
                stories = engine.asana.get_stories(gid)
                truth = execution_truth(authoritative, stories, now=engine.now())
                projection = {
                    "gid": gid,
                    "name": authoritative.get("name"),
                    "completed": bool(authoritative.get("completed")),
                    "completed_at": authoritative.get("completed_at"),
                    "modified_at": authoritative.get("modified_at"),
                    "memberships": list(authoritative.get("memberships") or []),
                    "dependencies": list(authoritative.get("dependencies") or []),
                    "dependents": list(authoritative.get("dependents") or []),
                    "execution": truth,
                    "provenance": {
                        "task": "asana-direct-read",
                        "stories": "asana-complete-story-history",
                    },
                }
                rollout = rollout_projection(reconstruct_rollout(stories, task_gid=gid))
                if rollout is not None:
                    projection["rollout"] = rollout
                tasks[gid] = projection
            except LifecycleError as exc:
                tasks[gid] = {
                    "gid": gid,
                    "error": str(exc),
                    "provenance": {"task": "asana-direct-read"},
                }
    scope = {
        "status": "COMPLETE" if not scope_errors else "INCOMPLETE",
        "projects": sorted(project_ids),
    }
    if scope_errors:
        scope["errors"] = scope_errors
    return list(tasks.values()), scope


def _write_task_projection_comments(
    engine: LifecycleEngine, tasks: Iterable[Mapping[str, Any]]
) -> None:
    if engine.asana is None:
        return
    for task in tasks:
        gid = str(task.get("gid") or "")
        if not gid or task.get("error"):
            continue
        projection = {
            key: task[key]
            for key in ("gid", "name", "completed", "modified_at", "execution", "rollout")
            if key in task
        }
        ensure_projection_comment(engine.asana, gid, projection)


def _task_projection_cycle(engine: LifecycleEngine, values: list[PRLifecycle]) -> list[dict[str, Any]]:
    tasks, _ = _task_observation_cycle(engine, values)
    _write_task_projection_comments(engine, tasks)
    return tasks


def _source_observation_cycle(
    engine: LifecycleEngine, values: list[PRLifecycle]
) -> dict[str, Any]:
    by_pr: dict[str, dict[str, Any]] = {}
    workstreams: list[dict[str, Any]] = []
    status = "COMPLETE"
    error = None
    candidate_reader = getattr(engine, "_workstream_candidates", None)
    candidates = {}
    if callable(candidate_reader):
        try:
            candidates = candidate_reader(values)
        except LifecycleError as exc:
            status = "INCOMPLETE"
            error = str(exc)

    for candidate in candidates.values():
        workstreams.append({
            "task": candidate.workstream_task,
            "candidate_id": candidate.candidate_id,
            "shape_id": candidate.shape_id,
            "ultimate_target": candidate.members[0].ultimate_target,
            "source_state": "LANDED" if candidate.source_complete else "NOT_LANDED",
            "members": [
                {
                    "pr": member.pr_number,
                    "head": member.head,
                    "base": member.base,
                    "publication_state": member.publication_state,
                }
                for member in candidate.members
            ],
        })
        for member in candidate.members:
            by_pr[str(member.pr_number)] = {
                "state": "LANDED" if member.publication_state == "landed" else "NOT_LANDED",
                "ultimate_target": member.ultimate_target,
                "publication_state": member.publication_state,
                "workstream_task": candidate.workstream_task,
                "candidate_id": candidate.candidate_id,
                "provenance": "existing-workstream-landing-model",
                **(
                    {"lineage_state": "MERGED_INTERMEDIATE_TARGET"}
                    if member.publication_state == "merged" and not candidate.source_complete
                    else {}
                ),
            }

    for value in values:
        key = str(value.number)
        if key in by_pr:
            continue
        if value.state == LifecycleState.MERGED and value.base == "main":
            by_pr[key] = {
                "state": "LANDED",
                "ultimate_target": "main",
                "publication_state": "landed",
                "provenance": "github-pr-merged-to-main",
            }
        elif value.base == "main":
            by_pr[key] = {
                "state": "NOT_LANDED",
                "ultimate_target": "main",
                "publication_state": (
                    "closed" if value.state == LifecycleState.CLOSED else "open"
                ),
                "provenance": "github-pr-state",
            }
        else:
            by_pr[key] = {
                "state": "UNKNOWN",
                "ultimate_target": None,
                "publication_state": (
                    "merged" if value.state == LifecycleState.MERGED else "unknown"
                ),
                "provenance": "ultimate-target-not-declared",
            }
    result = {
        "status": status,
        "pull_requests": by_pr,
        "workstreams": workstreams,
    }
    if error is not None:
        result["error"] = error
    return result


def _projection_health(
    engine: LifecycleEngine,
    *,
    previous_full_regression: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        controller = pr_lifecycle_controller._snapshot(pr_lifecycle_controller._paths())
    except (OSError, ValueError) as exc:
        controller = {"status": "unavailable", "error": str(exc)}
    try:
        payload = engine.github.full_regression_runs()
        runs = [item for item in payload.get("workflow_runs", []) if isinstance(item, Mapping)]
        latest = runs[0] if runs else {}
        full_regression = {
            key: latest.get(key)
            for key in (
                "id", "run_attempt", "event", "status", "conclusion", "head_sha",
                "updated_at", "html_url",
            )
            if latest.get(key) is not None
        }
        if latest.get("id") and latest.get("status") == "completed":
            previous = dict(previous_full_regression or {})
            previous_evidence = (
                previous.get("evidence")
                if isinstance(previous.get("evidence"), Mapping)
                else None
            )
            if (
                previous_evidence is not None
                and str(previous.get("id") or "") == str(latest.get("id") or "")
                and str(previous_evidence.get("run_id") or "") == str(latest.get("id") or "")
                and str(previous_evidence.get("main_sha") or "") == str(latest.get("head_sha") or "")
            ):
                full_regression["evidence"] = dict(previous_evidence)
                if previous.get("evidence_artifact_id") is not None:
                    full_regression["evidence_artifact_id"] = previous["evidence_artifact_id"]
                return controller, full_regression
            artifacts = engine.github.get_run_artifacts(int(latest["id"]))
            expected_name = f"full-regression-{latest.get('head_sha')}"
            artifact = next(
                (
                    item for item in artifacts
                    if item.get("name") == expected_name
                    and item.get("id")
                    and not item.get("expired")
                ),
                None,
            )
            if artifact is not None:
                archive = engine.github.download_artifact(int(artifact["id"]))
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    candidates = [
                        name for name in bundle.namelist()
                        if name == "evidence.json" or name.endswith("/full-regression/evidence.json")
                    ]
                    if len(candidates) != 1:
                        raise LifecycleError(
                            "full-regression artifact must contain exactly one evidence.json"
                        )
                    evidence = json.loads(bundle.read(candidates[0]))
                if not isinstance(evidence, Mapping):
                    raise LifecycleError("full-regression evidence is not an object")
                if evidence.get("schema") != "dish-full-regression-v1":
                    raise LifecycleError("full-regression evidence schema is invalid")
                if str(evidence.get("run_id") or "") != str(latest.get("id") or ""):
                    raise LifecycleError("full-regression evidence run ID does not match GitHub")
                if str(evidence.get("main_sha") or "") != str(latest.get("head_sha") or ""):
                    raise LifecycleError("full-regression evidence main SHA does not match GitHub")
                full_regression["evidence"] = dict(evidence)
                full_regression["evidence_artifact_id"] = artifact.get("id")
    except (LifecycleError, OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        full_regression = {"status": "unavailable", "error": str(exc)}
    return controller, full_regression


def _publish_projection(engine: LifecycleEngine, values: list[PRLifecycle], args: argparse.Namespace, *, mutate_tasks: bool) -> None:
    if args.projection_path is None:
        return
    tasks, task_scope = _task_observation_cycle(
        engine,
        values,
        configured_projects=(
            () if mutate_tasks else getattr(args, "observation_project_gids", ())
        ),
    )
    if mutate_tasks:
        _write_task_projection_comments(engine, tasks)
    else:
        # The project-wide Asana scan can take long enough for a PR or one of
        # its linked tasks to move.  Re-read the complete lifecycle authority
        # at the publication boundary and retain the previous atomic snapshot
        # unless both observations describe the same generation.
        refreshed = engine.status(include_closed=bool(getattr(args, "include_closed", False)))
        initial_generation = {value.number: value.json() for value in values}
        refreshed_generation = {value.number: value.json() for value in refreshed}
        if refreshed_generation != initial_generation:
            raise LifecycleError(
                "authority changed during projection generation; trustworthy snapshot publication refused"
            )
    source_observation = _source_observation_cycle(engine, values)
    previous_full_regression: Mapping[str, Any] = {}
    if args.projection_path.exists():
        try:
            previous = read_projection(args.projection_path)
        except (OSError, ValueError):
            pass
        else:
            if isinstance(previous.get("full_regression"), Mapping):
                previous_full_regression = previous["full_regression"]
    controller, full_regression = _projection_health(
        engine,
        previous_full_regression=previous_full_regression,
    )
    atomic_write(
        args.projection_path,
        build_projection(
            values,
            repository=args.repo,
            tasks=tasks,
            task_scope=task_scope,
            source_observation=source_observation,
            controller=controller,
            full_regression=full_regression,
        ),
    )

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_lifecycle", description=__doc__)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "marcogallotta/ai-tools"))
    parser.add_argument("--github-token", help=argparse.SUPPRESS)
    parser.add_argument("--asana-token", help=argparse.SUPPRESS)
    parser.add_argument("--github-api-root", default="https://api.github.com")
    parser.add_argument(
        "--http-timeout", type=float, default=10.0,
        help="maximum seconds for one GitHub or Asana HTTP read",
    )
    parser.add_argument("--workspace-api-root", default=WORKSPACE_API_ROOT)
    parser.add_argument("--workspace-token", help=argparse.SUPPRESS)
    parser.add_argument("--review-trigger-id")
    parser.add_argument("--worker-trigger-id")
    parser.add_argument("--local-reviewer", help="bounded local reviewer command; receives lifecycle JSON on stdin")
    parser.add_argument(
        "--implementation-fixer",
        help="existing implementation/fix consumer command; receives exact-head BLOCK dispatch JSON on stdin",
    )
    parser.add_argument(
        "--local-integration-launcher", "--local-integration-certifier",
        dest="local_integration_launcher",
        help=(
            "sole local Integration consumer for V1-A; receives exact-head certification or final "
            "Integration handoff JSON on stdin (legacy --local-integration-certifier spelling accepted)"
        ),
    )
    parser.add_argument(
        "--terminal-cleaner",
        help="repository-owned terminal cleanup command; defaults to tools/agent-worktree",
    )
    parser.add_argument(
        "--integration-authority", action="store_true",
        help="explicitly compose bounded local Integration after exact-head MERGE",
    )
    parser.add_argument(
        "--no-integration-capability", "--no-merge-capability",
        dest="no_integration_capability", action="store_true",
        help="declare that this host cannot launch the local Git-capable Integration consumer",
    )
    parser.add_argument("--merge-method", choices=["merge", "squash", "rebase"], default="squash")
    parser.add_argument("--projection-path", type=Path, help="atomic lifecycle JSON projection path")
    parser.add_argument(
        "--project-gid",
        dest="observation_project_gids",
        action="append",
        default=_configured_observation_projects(),
        help=(
            "Asana project included in the read-only task projection even when no current PR "
            f"establishes scope; repeatable or comma-separate {OBSERVATION_PROJECTS_ENV}"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="one-shot lifecycle status")
    status.add_argument("--format", choices=["json", "table"], default="table")
    status.add_argument("--include-closed", action="store_true")

    dispatch = sub.add_parser("dispatch", help="one idempotent routing/landing pass")
    dispatch.add_argument("--format", choices=["json", "table"], default="table")

    watch = sub.add_parser("watch", help="poll lifecycle state repeatedly")
    watch.add_argument("--interval", type=float, default=180.0)
    watch.add_argument("--dispatch", action="store_true", help="perform idempotent routing actions on each poll")
    watch.add_argument("--format", choices=["json", "table"], default="table")

    verify_bundle = sub.add_parser(
        "verify-local-bundle",
        help="verify the one receiver-readable exact candidate bundle for local publication completion",
    )
    verify_bundle.add_argument("--bundle", required=True, help="bundle basename expected in the downloads directory")
    verify_bundle.add_argument("--downloads-dir", type=Path, default=Path("~/Downloads"))
    verify_bundle.add_argument("--expected-head", required=True)
    verify_bundle.add_argument("--expected-tree", required=True)

    publication_route = sub.add_parser(
        "classify-publication-route",
        help="keep GitHub connector publication primary and select local bundle fallback only after an observed failed/degraded attempt",
    )
    publication_route.add_argument(
        "--connector-attempt-state",
        required=True,
        choices=("not-attempted", "working", "failing", "slow-or-manual", "unavailable"),
    )
    publication_route.add_argument(
        "--exact-candidate-bytes-available",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    publication_route.add_argument(
        "--attempt",
        action="append",
        default=[],
        help="concrete GitHub connector publication action attempted; repeat for multiple attempts",
    )
    publication_route.add_argument(
        "--stop-reason",
        help="exact observed failure/degradation reason when stopping remote publication",
    )

    finalize = sub.add_parser(
        "implementation-finalize",
        help="finalize one exact Implementation PR to authoritative review-ready state",
    )
    finalize.add_argument("--pr", type=int, required=True)
    finalize.add_argument("--expected-head", required=True)
    finalize.add_argument("--clear-publication-blocker", action="store_true")
    finalize.add_argument("--keep-draft-reason")

    checkpoint = sub.add_parser(
        "integration-checkpoint",
        help="update the current local Integration recovery claim from the fenced local consumer",
    )
    checkpoint.add_argument("--claim-path", required=True)
    checkpoint.add_argument("--claim-id", required=True)
    checkpoint.add_argument(
        "--phase", required=True,
        choices=[
            "claimed", "certifying", "reconciling", "reconciled", "premerge", "merged",
            "stopped-semantic", "failed-evidence", "returned", "head-changed",
        ],
    )
    checkpoint.add_argument("--worktree")
    checkpoint.add_argument("--current-head")
    checkpoint.add_argument("--main-sha")
    checkpoint.add_argument("--next-action")
    checkpoint.add_argument("--merge-sha")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "classify-publication-route":
            result = classify_publication_route(
                connector_attempt_state=args.connector_attempt_state,
                exact_candidate_bytes_available=args.exact_candidate_bytes_available,
                attempted_actions=args.attempt,
                stop_reason=args.stop_reason,
            )
            payload = asdict(result)
            if result.route != "DIRECT CONNECTOR":
                payload["operator_notice"] = render_publication_fallback_notice(result)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if result.route != FRESH_AUTHORING_REQUIRED else 2
        if args.command == "verify-local-bundle":
            result = classify_receiver_bundle(
                downloads_dir=args.downloads_dir,
                bundle_filename=args.bundle,
                expected_head=args.expected_head,
                expected_tree=args.expected_tree,
            )
            print(json.dumps(asdict(result), indent=2, sort_keys=True))
            return 0 if result.allowed else 2
        if args.command == "integration-checkpoint":
            value = checkpoint_claim(
                claim_path=args.claim_path,
                claim_id=args.claim_id,
                phase=args.phase,
                worktree=args.worktree,
                current_head=args.current_head,
                main_sha=args.main_sha,
                next_action=args.next_action,
                merge_sha=args.merge_sha,
            )
            print(json.dumps(value, indent=2, sort_keys=True))
            return 0
        engine, workspace, local, fixer, terminal_cleaner = _build_engine(args)
        if args.command == "implementation-finalize":
            result = engine.finalize_implementation_pr(
                args.pr,
                expected_head=args.expected_head,
                clear_publication_blocker=args.clear_publication_blocker,
                keep_draft_reason=args.keep_draft_reason,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("complete") is True else 2
        if args.command == "status":
            values = engine.status(include_closed=args.include_closed)
            _publish_projection(engine, values, args, mutate_tasks=False)
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values))
            return 0
        if args.command == "dispatch":
            values = engine.dispatch(
                workspace=workspace,
                local_reviewer=local,
                implementation_fixer=fixer,
                terminal_cleaner=terminal_cleaner,
                notify=_notification_printer,
            )
            _publish_projection(engine, values, args, mutate_tasks=True)
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values))
            return 0
        if args.interval < 1.0:
            raise LifecycleError("watch --interval must be at least 1 second")
        while True:
            values = (
                engine.dispatch(
                    workspace=workspace,
                    local_reviewer=local,
                    implementation_fixer=fixer,
                    terminal_cleaner=terminal_cleaner,
                    notify=_notification_printer,
                )
                if args.dispatch
                else engine.status()
            )
            _publish_projection(engine, values, args, mutate_tasks=bool(args.dispatch))
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (LifecycleError, pr_gate.GateError) as exc:
        print(f"pr_lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
