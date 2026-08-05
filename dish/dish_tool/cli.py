"""JSON-only argparse surface for the agent-facing ``dish`` executable."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from .backend import AsanaBackend
from .client_profiles import (
    add_profile_argument,
    argv_without_profile,
    profile_from_argv,
    resolve_client_profile,
)
from .commands import DishApplication
from .constants import DB_PATH
from .database import initialize_database
from .errors import DishRuleError
from .releases import configured_honest_path, resolve_release
from dish_service.client import DishServiceClient
from dish_service.database_ownership import ServiceDatabaseOwnership, database_process_lock_path
from dish_service.process_lock import DatabaseProcessLock
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
     Dish returns CONFIRMATION_REQUIRED and a durable intent challenge.
  2. Repeat start with a fresh request ID and either:
       --intent-challenge-id ID --intent-basis user_requested
     or:
       --intent-challenge-id ID --intent-basis agent_override --override-reason TEXT
  3. dish prepare SUBMISSION_ID --agent AGENT --model MODEL --file PATH

A passing `prepare` establishes deterministic structural conformance only -- it does not
authorize handoff by itself; Planning judgment still governs.

`allowed_actions` in every JSON response names the next legal command -- do not guess one.
Correct any agent-owned failure `prepare` reports and rerun it. On tool/protocol disagreement,
or any failed or uncertain tool result, stop, leave the task exactly as the tool left it, and
report; never repair it by hand. The protocol wins.

See `dish start --help` and `dish prepare --help` for the full argument reference.
""",
    "research": """\
dish research -- stage walkthrough (not a governed operation; this command only prints this text)

  1. dish start TASK_GID --agent AGENT --kind initial|change
  2. perform Research and self-review against the exact live task
  3. dish prepare SUBMISSION_ID --agent AGENT --model MODEL --file PATH

`--model` is caller-supplied, self-reported display metadata (e.g. `gpt-5.6-sol`). It is
labelled as self-reported in Researched by / Self-verified and is not authenticated runtime provenance.

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

  1. dish start TASK_GID --agent AGENT --kind verification --run-id RUN_ID
  2. review the exact frozen live task for semantic and provenance conformance
  3. dish approve SUBMISSION_ID --agent AGENT --model MODEL --correction none|small \\
       --semantic-review-complete --provenance-complete \\
       --reviewed-identity CONTENT_IDENTITY --run-id RUN_ID
     -- or --
     dish reject SUBMISSION_ID --agent AGENT --reason TEXT --route large|evidence|human-review \\
       [--model MODEL]

`--reviewed-identity` is the `content_identity` returned by `read`/`start` for the exact task
you reviewed; `approve` and `reject` both require the verifier identity recorded by `start`.
`--model` is caller-supplied, self-reported display metadata (e.g. `gpt-5.6-sol`). It is
labelled as self-reported in Verified by (and Self-verified for a Large or small correction),
not authenticated runtime provenance. `reject --route large` requires it; `approve` always
requires it.

A successful `approve` returns `submit` as the next action -- run it in the same pass:
  4. dish submit SUBMISSION_ID

A Large correction stays `pending-verification` for a fresh independent verifier; the
correcting verifier must not sign its own Large correction. The decision command must repeat
the exact verifier run ID recorded by `start` -- an operation ID, cycle ID, or agent-family
label alone is not authority.

`allowed_actions` in the JSON response names the next legal command. Do not retry an uncertain
mutation; preserve the complete JSON result and escalate to Marco for recovery.

See `dish start --help`, `dish approve --help`, `dish reject --help`, and `dish submit --help`
for the full argument reference.
""",
}


def build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="dish",
        description=(
            "Sole governed interface to protocol-managed Cooking tasks. Every "
            "protocol-managed task operation -- read, write, correction, signoff, and "
            "movement -- goes through this tool; the live Asana task is content authority, "
            "this tool never replaces protocol or agent judgment. Every governed command "
            "requires --agent claude|gpt|codex naming the agent family you are running as."
        ),
        epilog=(
            "For a stage workflow walkthrough (not a governed operation), run:\n"
            "  dish planning --help | dish research --help | dish verification --help"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_profile_argument(parser)
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

    section_tasks = subparsers.add_parser(
        "section-tasks", help="list the tasks currently placed in a Cooking project section"
    )
    section_tasks.add_argument("section_gid")
    section_tasks.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    section_tasks.add_argument(
        "--cursor", default=None, help="opaque next_cursor from a prior section-tasks page"
    )

    read = subparsers.add_parser("read", help="read the exact live task through the tool")
    read.add_argument("task_gid")
    read.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    inspect = subparsers.add_parser("inspect", help="inspect a prior tool operation's recorded state")
    inspect.add_argument("submission_id")
    inspect.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    inspect.add_argument("--request-id")

    proposals = subparsers.add_parser(
        "proposals", help="list Marco-approved semantic proposals ready for exact application"
    )
    proposals.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))

    apply_proposal = subparsers.add_parser(
        "apply-proposal", help="claim and apply one exact Marco-approved proposal bundle"
    )
    apply_proposal.add_argument("proposal_id")
    apply_proposal.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    apply_proposal.add_argument("--model", required=True)
    apply_proposal.add_argument("--run-id")
    apply_proposal.add_argument("--request-id")

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
    start.add_argument(
        "--prepared-operation-id",
        help="exact abandonment-created Planning/Research successor to claim",
    )
    start.add_argument(
        "--intent-challenge-id",
        help="durable challenge returned by the first Planning start call",
    )
    start.add_argument(
        "--intent-basis",
        choices=("user_requested", "agent_override"),
        help="explicit basis for a confirmed Planning start",
    )
    start.add_argument(
        "--override-reason",
        help="required non-blank reason when --intent-basis agent_override",
    )
    start.add_argument(
        "--target-operation-id",
        help="exact Verification operation returned by an abandonment action",
    )
    start.add_argument(
        "--target-cycle-id",
        help="exact Verification cycle returned by an abandonment action",
    )

    prepare = subparsers.add_parser(
        "prepare",
        help="submit the completed candidate for this operation and advance its state",
        description=(
            "Submit the completed candidate for this operation and advance its state. "
            "SUBMISSION_ID is the operation identifier `start` returned -- it appears there "
            "as both `submission_id` and `data.operation_id`; the two are the same value. "
            "A Planning submission (--kind planning) needs only --agent, --model, and --file; "
            "every other option below applies to a Research or change submission. --model is "
            "caller-supplied, self-reported display metadata; Dish labels it as self-reported "
            "in Researched by / Self-verified and does not authenticate it as runtime provenance."
        ),
    )
    prepare.add_argument("submission_id")
    prepare.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--file", dest="file_path", required=True)
    prepare.add_argument(
        "--material-classification",
        choices=("material", "non-material"),
        help=(
            "required only for a post-signoff change that alters the canonical body: "
            "classify that exact diff; Dish may force non-material to material when a "
            "protocol-defined material path changed"
        ),
    )

    approve = subparsers.add_parser(
        "approve",
        help="sign or small-correct a Verification candidate",
        description=(
            "Sign or small-correct a Verification candidate. --model is caller-supplied, "
            "self-reported display metadata; Dish labels it as self-reported in Verified by "
            "(and, for a small correction, Self-verified)."
        ),
    )
    approve.add_argument("submission_id")
    approve.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    approve.add_argument("--model", required=True)
    approve.add_argument("--file", dest="file_path")
    approve.add_argument(
        "--correction",
        required=True,
        choices=("none", "small"),
        help="none=sign as-is; small=fix-in-place execution-compliance correction, then sign in the same pass",
    )
    approve.add_argument("--reviewed-identity")
    approve.add_argument("--run-id")
    approve.add_argument("--semantic-review-complete", action="store_true")
    approve.add_argument("--provenance-complete", action="store_true")

    reject = subparsers.add_parser(
        "reject",
        help="stop signoff: a Large correction, Evidence gap, or Human Review",
        description=(
            "Stop signoff: a Large correction, Evidence gap, or Human Review. --route large "
            "requires --model as caller-supplied, self-reported display metadata; Dish labels it "
            "as self-reported in Self-verified."
        ),
    )
    reject.add_argument("submission_id")
    reject.add_argument("--agent", required=True, choices=("claude", "gpt", "codex"))
    reject.add_argument("--model")
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
    reject.add_argument("--run-id")

    submit = subparsers.add_parser(
        "submit", help="move a signed task to its recorded destination (run after a successful approve)"
    )
    submit.add_argument("submission_id")
    return parser


def build_application(profile: str | None = None):
    mode = os.environ.get("DISH_MODE", "").strip().lower()
    client_profile = resolve_client_profile(profile, admin=False)
    service_url = client_profile.service_url
    if mode not in {"", "local", "service"}:
        raise DishRuleError("INVALID_ARGUMENT", "DISH_MODE must be local or service", rule="dish_mode_invalid")
    live_mode = os.environ.get("DISH_LIVE_MODE", "").strip().lower() in {"1", "true", "yes"}
    if not mode and live_mode:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "live mode requires the shared dish service",
            rule="shared_service_required",
        )
    if not mode:
        if service_url:
            mode = "service"
        else:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "DISH_MODE is required; use service for live operation or local only for controlled development",
                rule="dish_mode_required",
            )
    if mode == "service":
        if not service_url:
            raise DishRuleError("INVALID_ARGUMENT", "DISH_SERVICE_URL is required in service mode", rule="service_url_required")
        return DishServiceClient(
            service_url,
            token=client_profile.token,
            run_id=os.environ.get("DISH_CLIENT_RUN_ID", ""),
            connect_timeout=float(os.environ.get("DISH_SERVICE_CLIENT_CONNECT_TIMEOUT", "10")),
            response_timeout=float(os.environ.get("DISH_SERVICE_CLIENT_RESPONSE_TIMEOUT", "600")),
        )
    if live_mode:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "live mode requires the shared dish service",
            rule="shared_service_required",
        )
    lock = DatabaseProcessLock(
        database_process_lock_path(DB_PATH), role="local-cli"
    ).acquire()
    try:
        ServiceDatabaseOwnership(DB_PATH).assert_local_access_allowed()
        honest_root = configured_honest_path()
        conn = initialize_database(DB_PATH)
        app = DishApplication(
            conn,
            AsanaBackend(),
            release_loader=lambda role=None: resolve_release(
                honest_root, protocol_role=role
            ),
        )
        app._database_process_lock = lock
        return app
    except Exception:
        lock.release()
        raise


def _argument_context(argv: Sequence[str]) -> dict[str, str | None]:
    argv = argv_without_profile(argv)
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
    command_arguments = argv_without_profile(arguments)

    if "-h" in arguments or "--help" in arguments:
        build_parser().parse_args(arguments)  # prints help and raises SystemExit(0)

    if len(command_arguments) == 1 and command_arguments[0] in TOPIC_COMMANDS:
        print(_TOPIC_WALKTHROUGHS[command_arguments[0]], end="")
        return 0

    context = _argument_context(arguments)
    owned_application = application is None
    try:
        requested_profile = profile_from_argv(arguments)
        app = application or (
            build_application(requested_profile)
            if requested_profile is not None
            else build_application()
        )
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
            parsed.pop("profile", None)
            result = app.execute(command, **parsed)
    except DishRuleError as exc:
        result = error_envelope(
            context["command"] or "unknown",
            exc,
            task_gid=context["task_gid"],
            submission_id=context["submission_id"],
        )
    except Exception as exc:
        error = DishRuleError(
            "INTERNAL_ERROR",
            "dish command failed",
            rule="command_failure",
            details={"error_type": type(exc).__name__},
        )
        result = error_envelope(
            context["command"] or "unknown",
            error,
            task_gid=context["task_gid"],
            submission_id=context["submission_id"],
        )
    try:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return exit_status(result["code"])
    finally:
        if owned_application:
            backend = getattr(app, "backend", None)
            close = getattr(backend, "close", None)
            if callable(close):
                close()
            conn = getattr(app, "conn", None)
            if conn is not None:
                conn.close()
            process_lock = getattr(app, "_database_process_lock", None)
            if process_lock is not None:
                process_lock.release()
