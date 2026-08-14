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
            lifecycle.state != LifecycleState.CHANGES_REQUESTED
            or not lifecycle.gate
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
) -> tuple[LifecycleEngine, WorkspaceAgentDispatcher | None, LocalReviewDispatcher, ImplementationFixDispatcher]:
    token = args.github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        raise LifecycleError("GitHub token is required via --github-token, GITHUB_TOKEN, or GH_TOKEN")
    github = GitHubREST(args.repo, token, api_root=args.github_api_root)
    asana_token = args.asana_token or os.getenv("ASANA_ACCESS_TOKEN")
    asana = AsanaREST(asana_token) if asana_token else None
    authority = args.integration_authority or os.getenv("DISH_INTEGRATION_AUTHORITY") == "bounded-reviewed-head"
    engine = LifecycleEngine(
        github,
        asana=asana,
        integration_authority=authority,
        integration_capable=not args.no_merge_capability,
        merge_method=args.merge_method,
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
    return engine, workspace, local, fixer


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
        residual = value.human_action or value.residual_reason or "-"
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
    widths = [4, 41, 10, 16, 16, 60]
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        engine, workspace, local, fixer = _build_engine(args)
        if args.command == "status":
            values = engine.status(include_closed=args.include_closed)
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values))
            return 0
        if args.command == "dispatch":
            values = engine.dispatch(
                workspace=workspace,
                local_reviewer=local,
                implementation_fixer=fixer,
                notify=_notification_printer,
            )
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
                    notify=_notification_printer,
                )
                if args.dispatch
                else engine.status()
            )
            print(_render_json(values, repository=args.repo) if args.format == "json" else _render_table(values), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (LifecycleError, pr_gate.GateError) as exc:
        print(f"pr_lifecycle: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
