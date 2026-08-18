#!/usr/bin/env python3
"""Durable PR lifecycle status and dispatch for Dish."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_lifecycle_support import *
from pr_lifecycle_helpers import *
from pr_lifecycle_helpers import _parse_time, _utcnow
from pr_lifecycle_external_replay import replay_external_dependency
from pr_lifecycle_engine_inspect import LifecycleInspectMixin
from pr_lifecycle_engine_actions import LifecycleActionsMixin
from pr_lifecycle_authoring_actions import LifecycleAuthoringActionsMixin
from pr_lifecycle_integration_certification import LocalIntegrationCertificationMixin
from pr_lifecycle_local_integration import LocalIntegrationLauncher, checkpoint_claim
from pr_lifecycle_terminal import TerminalCleanupDispatcher
from pr_lifecycle_operator import action_first_status
from pr_lifecycle_projection import atomic_write, build_projection
from pr_lifecycle_task_state import execution_truth, ensure_projection_comment
from pr_lifecycle_rollout import reconstruct as reconstruct_rollout, rollout_projection
import pr_lifecycle_controller
from pr_mutation_broker import (
    BrokerError, artifact_name as broker_artifact_name, broker_filter_event, finalize_broker_event,
    prepare_broker_event, route_policy_from_json, write_github_outputs,
)

class LifecycleEngine(
    LocalIntegrationCertificationMixin,
    LifecycleInspectMixin,
    LifecycleAuthoringActionsMixin,
    LifecycleActionsMixin,
):
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
    broker_enabled = os.getenv("DISH_MUTATION_BROKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    local_integration_command = (
        args.local_integration_launcher or os.getenv("DISH_LOCAL_INTEGRATION_COMMAND")
    )
    integration_capable = bool(local_integration_command) and not args.no_integration_capability
    repository_id = None
    if broker_enabled:
        raw_repo_id = os.getenv("GITHUB_REPOSITORY_ID")
        repository_id = int(raw_repo_id) if raw_repo_id else github.get_repository_id()
    implementation_route = os.getenv("DISH_MUTATION_BROKER_IMPLEMENTATION_ROUTE")
    legacy_fix_route = os.getenv("DISH_MUTATION_BROKER_FIX_ROUTE") or implementation_route
    broker_routes = {
        "implementation": implementation_route,
        "fix": legacy_fix_route,
        "fix-chatgpt": os.getenv("DISH_MUTATION_BROKER_CHATGPT_IMPLEMENTATION_ROUTE") or legacy_fix_route,
        "fix-local": os.getenv("DISH_MUTATION_BROKER_LOCAL_IMPLEMENTATION_ROUTE"),
    }
    engine = LifecycleEngine(
        github,
        asana=asana,
        integration_authority=authority,
        integration_capable=integration_capable,
        merge_method=args.merge_method,
        mutation_broker_enabled=broker_enabled,
        mutation_broker_repository_id=repository_id,
        mutation_broker_routes={k: v for k, v in broker_routes.items() if v},
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




def _task_projection_cycle(engine: LifecycleEngine, values: list[PRLifecycle]) -> list[dict[str, Any]]:
    if engine.asana is None:
        return []
    project_ids: set[str] = set()
    for value in values:
        for task in value.asana:
            for membership in task.get("memberships") or []:
                if isinstance(membership, Mapping) and isinstance(membership.get("project"), Mapping):
                    gid = str(membership["project"].get("gid") or "")
                    if gid:
                        project_ids.add(gid)
    tasks: dict[str, dict[str, Any]] = {}
    for project_gid in sorted(project_ids):
        for task in engine.asana.list_project_tasks(project_gid):
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
                    "modified_at": authoritative.get("modified_at"),
                    "execution": truth,
                }
                rollout = rollout_projection(reconstruct_rollout(stories, task_gid=gid))
                if rollout is not None:
                    projection["rollout"] = rollout
                ensure_projection_comment(engine.asana, gid, projection)
                tasks[gid] = projection
            except LifecycleError as exc:
                tasks[gid] = {"gid": gid, "error": str(exc)}
    return list(tasks.values())


def _projection_health(engine: LifecycleEngine) -> tuple[dict[str, Any], dict[str, Any]]:
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
            for key in ("id", "status", "conclusion", "head_sha", "updated_at", "html_url")
            if latest.get(key) is not None
        }
    except LifecycleError as exc:
        full_regression = {"status": "unavailable", "error": str(exc)}
    return controller, full_regression


def _publish_projection(engine: LifecycleEngine, values: list[PRLifecycle], args: argparse.Namespace, *, mutate_tasks: bool) -> None:
    if args.projection_path is None:
        return
    tasks = _task_projection_cycle(engine, values) if mutate_tasks else []
    controller, full_regression = _projection_health(engine)
    atomic_write(
        args.projection_path,
        build_projection(
            values,
            repository=args.repo,
            tasks=tasks,
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

    broker_filter = sub.add_parser("broker-filter", help="validate one issue_comment mutation request without authority reads")
    broker_filter.add_argument("--event-path", required=True)
    broker_filter.add_argument("--github-output", required=True)

    broker_prepare = sub.add_parser("broker-prepare", help="re-read authority and create one provisional broker event")
    broker_prepare.add_argument("--request-comment-id", required=True, type=int)
    broker_prepare.add_argument("--repository-id", required=True)
    broker_prepare.add_argument("--run-id", required=True, type=int)
    broker_prepare.add_argument("--run-attempt", required=True, type=int)
    broker_prepare.add_argument("--trusted-source-sha", required=True)
    broker_prepare.add_argument("--proof-path", required=True)
    broker_prepare.add_argument("--github-output", required=True)

    broker_finalize = sub.add_parser("broker-finalize", help="bind uploaded proof transport metadata to the same broker event comment")
    broker_finalize.add_argument("--comment-id", required=True, type=int)
    broker_finalize.add_argument("--artifact-id", required=True, type=int)
    broker_finalize.add_argument("--artifact-digest", required=True)
    broker_finalize.add_argument("--repository-id", required=True)
    broker_finalize.add_argument("--run-id", required=True, type=int)
    broker_finalize.add_argument("--run-attempt", required=True, type=int)

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
        if args.command == "broker-filter":
            value = json.loads(Path(args.event_path).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise BrokerError("GitHub event payload must be a JSON object")
            valid, pr_number, comment_id = broker_filter_event(value)
            outputs = {"valid": valid}
            if valid:
                outputs.update({"pr_number": pr_number, "request_comment_id": comment_id})
            write_github_outputs(args.github_output, outputs)
            return 0

        engine, workspace, local, fixer, terminal_cleaner = _build_engine(args)
        if args.command == "broker-prepare":
            policy = route_policy_from_json(os.getenv("DISH_MUTATION_BROKER_ALLOWED_ROUTES_JSON"))
            event = prepare_broker_event(
                engine=engine,
                request_comment_id=args.request_comment_id,
                repository_id=args.repository_id,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                trusted_source_sha=args.trusted_source_sha,
                proof_path=args.proof_path,
                route_policy=policy,
            )
            write_github_outputs(
                args.github_output,
                {
                    "requires_proof": True,
                    "comment_id": event.comment_id,
                    "artifact_name": broker_artifact_name(args.run_id, args.run_attempt, event.comment_id),
                    "grant_id": event.payload["grant_id"],
                    "generation": event.payload["generation"],
                    "event_digest": event.event_digest,
                },
            )
            return 0
        if args.command == "broker-finalize":
            final = finalize_broker_event(
                github=engine.github,
                comment_id=args.comment_id,
                artifact_id=args.artifact_id,
                artifact_digest=args.artifact_digest,
                repository_id=args.repository_id,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
            )
            # A successful exact run attempt is verified by future readers; finalize only
            # proves same-comment transport readback inside this still-running attempt.
            print(json.dumps({"comment_id": final.comment_id, "event_digest": final.event_digest}, sort_keys=True))
            return 0
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
    except (LifecycleError, BrokerError, pr_gate.GateError) as exc:
        print(f"pr_lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
