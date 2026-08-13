from __future__ import annotations

import argparse

from .common import AgentWorktreeError, DISPOSITIONS, GitRunner, reject_repository_overrides
from .operations import emit
from .ownership import command_claim, require_active_claim
from .publish_cleanup import command_cleanup, command_exec, command_publish, command_verify_handoff
from .start_resume import command_adopt, command_resume, command_start, command_status
from .state import load_task_state

def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit one machine-readable JSON object")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    claim = sub.add_parser("claim", help="exclusively claim task/branch ownership for one dispatched local-agent process")
    claim.add_argument("--task", required=True)
    claim.add_argument("--branch", required=True)
    claim.add_argument("--agent-id", required=True)
    claim.add_argument("--repo", default=".", help="existing checkout/worktree used to identify the shared repository")
    claim.add_argument("--takeover", action="store_true", help="explicitly accept a stale/released prior owner after orchestration handoff")
    claim.add_argument("--pr-number", type=int)
    claim.add_argument("--pr-head")
    claim.add_argument("--pr-lease-state", choices=("active", "none"))
    claim.add_argument("--pr-lease-id")
    claim.add_argument("argv", nargs=argparse.REMAINDER)

    start = sub.add_parser("start", help="create or resume the durable task-owned worktree")
    start.add_argument("--task", required=True)
    start.add_argument("--branch", required=True)
    start.add_argument("--base-ref", required=True)
    start.add_argument("--base", required=True)
    start.add_argument("--agent-id")
    start.add_argument("--repo", default=".", help="existing checkout/worktree used to identify the shared repository")
    add_json_flag(start)

    adopt = sub.add_parser("adopt", help="adopt an explicitly handed-off existing remote agent branch")
    adopt.add_argument("--task", required=True)
    adopt.add_argument("--branch", required=True)
    adopt.add_argument("--base-ref", required=True)
    adopt.add_argument("--base", required=True)
    adopt.add_argument("--expected-head", required=True)
    adopt.add_argument("--agent-id")
    adopt.add_argument("--repo", default=".", help="existing checkout/worktree used to identify the shared repository")
    add_json_flag(adopt)

    resume = sub.add_parser("resume", help="verify and resume preserved task-owned implementation state")
    resume.add_argument("--task", required=True)
    resume.add_argument("--agent-id")
    resume.add_argument("--takeover", action="store_true")
    add_json_flag(resume)

    status = sub.add_parser("status", help="read-only local task/worktree identity report")
    status.add_argument("--task", required=True)
    add_json_flag(status)

    publish = sub.add_parser("publish", help="push only the explicit owned branch refspec and verify remote HEAD")
    publish.add_argument("--task", required=True)
    add_json_flag(publish)

    handoff = sub.add_parser("verify-handoff", help="verify durable remote handoff without chasing a moved target branch")
    handoff.add_argument("--task", required=True)
    add_json_flag(handoff)

    cleanup = sub.add_parser("cleanup", help="conservatively remove the linked worktree after an established disposition")
    cleanup.add_argument("--task", required=True)
    cleanup.add_argument("--disposition", required=True, choices=sorted(DISPOSITIONS))
    add_json_flag(cleanup)

    execute = sub.add_parser("exec", help="execute a host command with cwd rooted in the verified owned worktree")
    execute.add_argument("--task", required=True)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    reject_repository_overrides()
    args = build_parser().parse_args(argv)
    runner = GitRunner()
    try:
        if args.command == "claim":
            return command_claim(args, runner)
        if args.command == "start":
            require_active_claim(args.task, args.branch, args.agent_id)
            payload = command_start(args, runner)
        elif args.command == "adopt":
            claim = require_active_claim(args.task, args.branch, args.agent_id, require_pr=True)
            claim_pr = claim.get("pr")
            if not isinstance(claim_pr, dict) or claim_pr.get("head") != args.expected_head:
                from .common import fail
                fail(
                    "OWNERSHIP_AMBIGUOUS",
                    "adopt expected head does not match the exact PR head bound to the live dispatch claim",
                )
            payload = command_adopt(args, runner)
        elif args.command == "resume":
            state = load_task_state(args.task)
            require_active_claim(
                args.task, str(state["branch"]), args.agent_id, allow_takeover=bool(args.takeover)
            )
            payload = command_resume(args, runner)
        elif args.command == "status":
            payload = command_status(args, runner)
        elif args.command == "publish":
            payload = command_publish(args, runner)
        elif args.command == "verify-handoff":
            payload = command_verify_handoff(args, runner)
        elif args.command == "cleanup":
            payload = command_cleanup(args, runner)
        elif args.command == "exec":
            command_exec(args, runner)
        else:
            raise AssertionError(args.command)
        emit(payload, getattr(args, "json", False))
        if args.command == "status" and not payload.get("ok", False):
            return 1
        return 0
    finally:
        runner.close()
