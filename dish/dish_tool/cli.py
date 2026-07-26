"""JSON-only argparse surface for the agent-facing ``dish`` executable."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .backend import AsanaBackend
from .commands import DishApplication
from .constants import DEFAULT_DB_PATH
from .database import initialize_database
from .errors import DishRuleError
from .releases import configured_honest_path, resolve_release
from .results import error_envelope, exit_status


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DishRuleError(
            "INVALID_ARGUMENT", message, rule="invalid_arguments"
        )


TOPIC_COMMANDS = ("planning", "research", "verification")

_TOPIC_WALKTHROUGHS: dict[str, str] = {
    "planning": """\
dish planning -- stage walkthrough (not a governed operation; this command only prints this text)

Planning's only tool responsibility is the boundary check before handing the exact live
Planning task to Research:

  1. dish start TASK_GID --agent AGENT --kind planning
  2. dish prepare SUBMISSION_ID --agent AGENT --file PATH

A passing `prepare` establishes deterministic structural conformance only -- it does not
authorize handoff by itself; Planning judgment still governs.

`allowed_actions` in every JSON response names the next legal command -- do not guess one.
Correct any agent-owned failure `prepare` reports and rerun it. On tool/protocol disagreement,
preserve the live task and stop the transition; the protocol wins.

See `dish start --help` and `dish prepare --help` for the full argument reference.
""",
    "research": """\
dish research -- stage walkthrough (not a governed operation; this command only prints this text)

  1. dish start TASK_GID --agent AGENT --kind initial|change
  2. perform Research and self-review against the exact live task
  3. dish prepare SUBMISSION_ID --agent AGENT --file PATH

A successful `prepare` writes and confirms the complete `pending-verification` task before any
Research Queue -> Verification Queue move -- the tool owns that move, not the agent.

Correct any agent-owned VALIDATION_FAILED finding and rerun `prepare`; do not treat a tool
failure as Evidence or Human Review. `allowed_actions` in the JSON response names the next
legal command.

A later material edit after signoff begins a new operation:
  dish start TASK_GID --agent AGENT --kind change --change-level small|large --change-reason TEXT

See `dish start --help` and `dish prepare --help` for the full argument reference.
""",
    "verification": """\
dish verification -- stage walkthrough (not a governed operation; this command only prints this text)

  1. dish start TASK_GID --agent AGENT --kind verification
  2. review the exact frozen live task for semantic and provenance conformance
  3. dish approve SUBMISSION_ID --agent AGENT --correction none|small \\
       --semantic-review-complete --provenance-complete
     -- or --
     dish reject SUBMISSION_ID --agent AGENT --reason TEXT --route large|evidence|human-review

A successful `approve` returns `submit` as the next action -- run it in the same pass:
  4. dish submit SUBMISSION_ID

A Large correction stays `pending-verification` for a fresh independent verifier; the
correcting verifier must not sign its own Large correction. The decision command must repeat
the exact verifier run ID or independence attestation recorded by `start` -- an agent-family
label alone is not authority.

`allowed_actions` in the JSON response names the next legal command. Do not retry an uncertain
mutation; preserve the complete JSON result and escalate to Marco for recovery.

See `dish start --help`, `dish approve --help`, `dish reject --help`, and `dish submit --help`
for the full argument reference.
""",
}


def _add_title_declaration(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dish-name")
    parser.add_argument("--recognition")
    parser.add_argument("--role", dest="roles", action="append")
    parser.add_argument("--no-role-tags", action="store_true")
    parser.add_argument("--blocker", dest="blockers", action="append")
    parser.add_argument("--no-blockers", action="store_true")


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="dish",
        description=(
            "Sole governed interface to protocol-managed Cooking tasks. Every read, write, "
            "correction, signoff, and movement goes through this tool; the live Asana task "
            "is content authority, this tool never replaces protocol or agent judgment."
        ),
        epilog=(
            "For a stage workflow walkthrough (not a governed operation), run:\n"
            "  dish planning --help | dish research --help | dish verification --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for topic in TOPIC_COMMANDS:
        subparsers.add_parser(
            topic,
            help="stage walkthrough (not a governed operation)",
            description=_TOPIC_WALKTHROUGHS[topic],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    create = subparsers.add_parser("create", help="open a new canonical Cooking task")
    create.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    create.add_argument("--title", required=True)

    sections = subparsers.add_parser("sections", help="list Cooking project sections and gids")
    sections.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    read = subparsers.add_parser("read", help="read the exact live task through the tool")
    read.add_argument("task_gid")
    read.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    inspect = subparsers.add_parser("inspect", help="inspect a prior tool operation's recorded state")
    inspect.add_argument("submission_id")
    inspect.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    start = subparsers.add_parser(
        "start", help="open a Planning/Research/Verification/change operation on a task"
    )
    start.add_argument("task_gid")
    start.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    start.add_argument(
        "--kind",
        required=True,
        choices=("planning", "initial", "change", "verification"),
        help=(
            "planning=Planning boundary check; initial=first Research construction; "
            "change=a later material or non-material edit after signoff; "
            "verification=a fresh independent Verification pass"
        ),
    )
    start.add_argument("--run-id")
    start.add_argument("--independence-attestation")
    start.add_argument(
        "--change-level",
        choices=("small", "large"),
        help="required with --kind change: small preserves settled construction, large materially changes it",
    )
    start.add_argument("--change-reason")

    prepare = subparsers.add_parser(
        "prepare", help="submit the completed candidate for this operation and advance its state"
    )
    prepare.add_argument("submission_id")
    prepare.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    prepare.add_argument("--file", dest="file_path", required=True)
    prepare.add_argument("--exemption-revision")
    prepare.add_argument("--material-classification", choices=("material", "non-material"))
    _add_title_declaration(prepare)

    approve = subparsers.add_parser("approve", help="sign or small-correct a Verification candidate")
    approve.add_argument("submission_id")
    approve.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    approve.add_argument("--file", dest="file_path")
    approve.add_argument(
        "--correction",
        required=True,
        choices=("none", "small"),
        help="none=sign as-is; small=fix-in-place execution-compliance correction, then sign in the same pass",
    )
    approve.add_argument("--reviewed-identity")
    approve.add_argument("--run-id")
    approve.add_argument("--independence-attestation")
    approve.add_argument("--semantic-review-complete", action="store_true")
    approve.add_argument("--provenance-complete", action="store_true")
    _add_title_declaration(approve)

    reject = subparsers.add_parser(
        "reject", help="stop signoff: a Large correction, Evidence gap, or Human Review"
    )
    reject.add_argument("submission_id")
    reject.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    reject.add_argument("--reason", required=True)
    reject.add_argument(
        "--route",
        choices=("large", "evidence", "human-review"),
        help=(
            "large=materially changes identity/quantities/route, needs a fresh independent verifier; "
            "evidence=only Marco can supply a missing fact; "
            "human-review=only Marco can choose, authorize, classify, or accept a consequence"
        ),
    )
    reject.add_argument("--file", dest="file_path")
    reject.add_argument("--resume-status", choices=("pending-verification", "pending-research"))
    reject.add_argument("--changed-since-prior")
    reject.add_argument("--take-ownership", action="store_true")
    reject.add_argument("--run-id")
    reject.add_argument("--independence-attestation")

    submit = subparsers.add_parser(
        "submit", help="move a signed task to its recorded destination (run after a successful approve)"
    )
    submit.add_argument("submission_id")
    submit.add_argument("--file", dest="file_path")
    return parser


def build_application() -> DishApplication:
    db_path = Path(os.environ.get("DISH_DB_PATH", str(DEFAULT_DB_PATH))).expanduser()
    honest_root = configured_honest_path()
    conn = initialize_database(db_path)
    return DishApplication(
        conn,
        AsanaBackend(),
        release_loader=lambda role=None: resolve_release(
            honest_root, protocol_role=role
        ),
    )


def _argument_context(argv: Sequence[str]) -> dict[str, str | None]:
    command = argv[0] if argv and not argv[0].startswith("-") else "unknown"
    agent = None
    if "--agent" in argv:
        index = argv.index("--agent")
        if index + 1 < len(argv):
            agent = argv[index + 1]
    task_gid = None
    submission_id = None
    if command in {"read", "start"} and len(argv) > 1 and not argv[1].startswith("-"):
        task_gid = argv[1]
    if (
        command in {"inspect", "prepare", "approve", "reject", "submit"}
        and len(argv) > 1
        and not argv[1].startswith("-")
    ):
        submission_id = argv[1]
    return {
        "command": command,
        "agent": agent,
        "task_gid": task_gid,
        "submission_id": submission_id,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    application: DishApplication | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    if "-h" in arguments or "--help" in arguments:
        build_parser().parse_args(arguments)  # prints help and raises SystemExit(0)

    if len(arguments) == 1 and arguments[0] in TOPIC_COMMANDS:
        print(_TOPIC_WALKTHROUGHS[arguments[0]], end="")
        return 0

    context = _argument_context(arguments)
    owned_application = application is None
    try:
        app = application or build_application()
    except DishRuleError as exc:
        result = error_envelope(context["command"] or "unknown", exc)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])
    except Exception:
        error = DishRuleError(
            "INTERNAL_ERROR",
            "dish failed during startup",
            rule="startup_failure",
        )
        result = error_envelope(context["command"] or "unknown", error)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])

    try:
        try:
            parsed = vars(build_parser().parse_args(arguments))
        except DishRuleError as exc:
            result = app.record_argument_failure(
                context["command"] or "unknown",
                exc,
                agent=context["agent"],
                task_gid=context["task_gid"],
                submission_id=context["submission_id"],
            )
        else:
            command = parsed.pop("command")
            result = app.execute(command, **parsed)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])
    finally:
        if owned_application:
            app.conn.close()
