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
from pr_lifecycle_helpers import _parse_time
from pr_lifecycle_external_replay import replay_external_dependency
from pr_lifecycle_engine_inspect import LifecycleInspectMixin
from pr_lifecycle_engine_actions import LifecycleActionsMixin
from pr_lifecycle_authoring_actions import LifecycleAuthoringActionsMixin
from pr_lifecycle_integration_certification import LocalIntegrationCertificationMixin
from pr_lifecycle_terminal import TerminalCleanupDispatcher
from pr_lifecycle_operator import action_first_status
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
    ImplementationFixDispatcher, TerminalCleanupDispatcher,
]:
    token = args.github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise LifecycleError("GitHub token is required via --github-token, GITHUB_TOKEN, or GH_TOKEN")
    github = GitHubREST(args.repo, token, api_root=args.github_api_root)
    asana_token = args.asana_token or os.getenv("ASANA_ACCESS_TOKEN")
    asana = AsanaREST(asana_token) if asana_token else None
    authority = args.integration_authority or os.getenv("DISH_INTEGRATION_AUTHORITY") == "bounded-reviewed-head"
    broker_enabled = os.getenv("DISH_MUTATION_BROKER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    repository_id = None
    if broker_enabled:
        raw_repo_id = os.getenv("GITHUB_REPOSITORY_ID")
        repository_id = int(raw_repo_id) if raw_repo_id else github.get_repository_id()
    implementation_route = os.getenv("DISH_MUTATION_BROKER_IMPLEMENTATION_ROUTE")
    integration_route = os.getenv("DISH_MUTATION_BROKER_INTEGRATION_ROUTE")
    broker_routes = {
        "implementation": implementation_route,
        "fix": os.getenv("DISH_MUTATION_BROKER_FIX_ROUTE") or implementation_route,
        "integration-reconcile": os.getenv("DISH_MUTATION_BROKER_RECONCILE_ROUTE") or integration_route,
        "merge": os.getenv("DISH_MUTATION_BROKER_MERGE_ROUTE") or integration_route,
    }
    engine = LifecycleEngine(
        github,
        asana=asana,
        integration_authority=authority,
        integration_capable=not args.no_merge_capability,
        merge_method=args.merge_method,
        mutation_broker_enabled=broker_enabled,
        mutation_broker_repository_id=repository_id,
        mutation_broker_routes={k: v for k, v in broker_routes.items() if v},
    )
    workspace_token = args.workspace_token or os.getenv("DISH_WORKSPACE_AGENT_ACCESS_TOKEN")
    review_trigger = args.review_trigger_id or os.getenv("DISH_REVIEW_API_TRIGGER_ID")
    workspace = None
    if workspace_token or review_trigger:
        workspace = WorkspaceAgentDispatcher(
            access_token=workspace_token or "",
            review_trigger_id=review_trigger,
            api_root=args.workspace_api_root,
        )
    local = LocalReviewDispatcher(args.local_reviewer or os.getenv("DISH_LOCAL_REVIEW_COMMAND"))
    fixer = ImplementationFixDispatcher(
        args.implementation_fixer or os.getenv("DISH_IMPLEMENTATION_FIX_COMMAND")
    )
    certifier = ImplementationFixDispatcher(
        args.local_integration_certifier or os.getenv("DISH_LOCAL_INTEGRATION_CERTIFICATION_COMMAND")
    )
    engine.local_integration_certifier = certifier
    engine.integration_reconciler = ImplementationFixDispatcher(
        os.getenv("DISH_INTEGRATION_RECONCILE_COMMAND")
    )
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr_lifecycle", description=__doc__)
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "marcogallotta/ai-tools"))
    parser.add_argument("--github-token", help=argparse.SUPPRESS)
    parser.add_argument("--asana-token", help=argparse.SUPPRESS)
    parser.add_argument("--github-api-root", default="https://api.github.com")
    parser.add_argument("--workspace-api-root", default=WORKSPACE_API_ROOT)
    parser.add_argument("--workspace-token", help=argparse.SUPPRESS)
    parser.add_argument("--review-trigger-id")
    parser.add_argument("--local-reviewer", help="bounded local reviewer command; receives lifecycle JSON on stdin")
    parser.add_argument(
        "--implementation-fixer",
        help="existing implementation/fix consumer command; receives exact-head BLOCK dispatch JSON on stdin",
    )
    parser.add_argument(
        "--local-integration-certifier",
        help="local Integration consumer; receives complete exact-head certification handoff JSON on stdin",
    )
    parser.add_argument(
        "--terminal-cleaner",
        help="repository-owned terminal cleanup command; defaults to tools/agent-worktree",
    )
    parser.add_argument("--integration-authority", action="store_true", help="explicitly compose bounded Integration after exact-head MERGE")
    parser.add_argument("--no-merge-capability", action="store_true", help="declare that this host cannot perform GitHub merge")
    parser.add_argument("--merge-method", choices=["merge", "squash", "rebase"], default="squash")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
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
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values))
            return 0
        if args.command == "dispatch":
            values = engine.dispatch(
                include_closed=True,
                workspace=workspace,
                local_reviewer=local,
                implementation_fixer=fixer,
                terminal_cleaner=terminal_cleaner,
                notify=_notification_printer,
            )
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values))
            return 0
        if args.interval < 1.0:
            raise LifecycleError("watch --interval must be at least 1 second")
        while True:
            values = (
                engine.dispatch(
                    include_closed=True,
                    workspace=workspace,
                    local_reviewer=local,
                    implementation_fixer=fixer,
                    terminal_cleaner=terminal_cleaner,
                    notify=_notification_printer,
                )
                if args.dispatch
                else engine.status()
            )
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (LifecycleError, BrokerError, pr_gate.GateError) as exc:
        print(f"pr_lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
