"""Conservative terminal PR disposition and cleanup helpers."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
import subprocess
from typing import Any

from pr_lifecycle_support import LifecycleError, PRLifecycle

TERMINAL_DISPOSITION_MARKER = "dish-terminal-disposition:v1"
TERMINAL_CLEANUP_MARKER = "dish-terminal-cleanup:v1"

_PR_TOKEN_RE = re.compile(r"\bPR\s*#(?P<number>\d+)\b", re.IGNORECASE)
_TASK_LEVEL_RE = re.compile(r"^\s*(?P<kind>SUPERSEDED|ABANDONED|REPLACED)\b", re.IGNORECASE)
_EXPLICIT_PR_TERMINAL_RE = re.compile(
    r"(?:supersed|abandon|replac|do\s+not\s+(?:touch/)?revive|do\s+not\s+revive/land|not[- ]to[- ]be[- ]revived)",
    re.IGNORECASE,
)
_REPLACEMENT_PR_RE = re.compile(
    r"(?:replaced|superseded)\s+by\s+PR\s*#(?P<pr>\d+)|replacement\s+PR\s*#(?P<replacement>\d+)",
    re.IGNORECASE,
)
_REPLACEMENT_TASK_RE = re.compile(
    r"(?:current\s+authority\s+is|replacement\s+task|folded\s+into)\b[^\n]{0,160}?\btask\s+(?P<task>\d{16})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TerminalDecision:
    disposition: str
    task_gid: str
    task_url: str | None
    reason: str
    replacement_pr: int | None = None
    replacement_task: str | None = None


def _replacement_lineage(notes: str, *, current_pr: int) -> tuple[int | None, str | None]:
    replacement_pr = None
    for match in _REPLACEMENT_PR_RE.finditer(notes):
        raw = match.group("pr") or match.group("replacement")
        if raw and int(raw) != current_pr:
            replacement_pr = int(raw)
            break
    task_match = _REPLACEMENT_TASK_RE.search(notes)
    replacement_task = task_match.group("task") if task_match else None
    return replacement_pr, replacement_task


def asana_terminal_decision(lifecycle: PRLifecycle) -> TerminalDecision | None:
    """Return only explicit current Asana abandonment/supersession authority.

    Generic completion, age/staleness, parking, and temporary blockers are not terminal
    authority. Legacy prose is accepted only when the current task record names the exact
    PR and uses explicit no-revive/supersession language, covering the #45/#54 incidents.
    """
    for task in lifecycle.asana:
        gid = str(task.get("gid") or "")
        if not gid or task.get("error"):
            continue
        name = str(task.get("name") or "")
        notes = str(task.get("notes") or "")
        combined = f"{name}\n{notes}"
        replacement_pr, replacement_task = _replacement_lineage(notes, current_pr=lifecycle.number)

        level = _TASK_LEVEL_RE.match(name)
        if level and bool(task.get("completed")):
            kind = level.group("kind").lower()
            disposition = "abandoned" if kind == "abandoned" else "superseded"
            return TerminalDecision(
                disposition=disposition,
                task_gid=gid,
                task_url=task.get("permalink_url"),
                reason=f"owning Asana task is completed with explicit {kind} disposition",
                replacement_pr=replacement_pr,
                replacement_task=replacement_task,
            )

        # PR-specific authority can live in an ongoing umbrella task. Require the exact
        # PR token and explicit terminal/no-revive language in the same line or sentence.
        for line in combined.splitlines():
            numbers = {int(match.group("number")) for match in _PR_TOKEN_RE.finditer(line)}
            if lifecycle.number in numbers and _EXPLICIT_PR_TERMINAL_RE.search(line):
                disposition = "abandoned" if re.search(r"abandon", line, re.IGNORECASE) else "superseded"
                return TerminalDecision(
                    disposition=disposition,
                    task_gid=gid,
                    task_url=task.get("permalink_url"),
                    reason="owning Asana task explicitly marks this PR as terminal/not-to-be-revived",
                    replacement_pr=replacement_pr,
                    replacement_task=replacement_task,
                )
    return None


def disposition_marker(decision: TerminalDecision, lifecycle: PRLifecycle) -> str:
    fields = [
        TERMINAL_DISPOSITION_MARKER,
        f"disposition={decision.disposition}",
        f"head={lifecycle.head}",
        f"task={decision.task_gid}",
    ]
    if decision.replacement_pr is not None:
        fields.append(f"replacement_pr={decision.replacement_pr}")
    if decision.replacement_task is not None:
        fields.append(f"replacement_task={decision.replacement_task}")
    return "<!-- " + " ".join(fields) + " -->"


def cleanup_marker(lifecycle: PRLifecycle, disposition: str) -> str:
    return (
        f"<!-- {TERMINAL_CLEANUP_MARKER} disposition={disposition} "
        f"head={lifecycle.head} branch={lifecycle.branch} result=complete -->"
    )


def comment_has_marker(comments: list[dict[str, Any]], marker: str) -> bool:
    return any(marker in str(comment.get("body") or "") for comment in comments)


class TerminalCleanupDispatcher:
    """Invoke agent-worktree terminal cleanup with exact PR/branch/head identity."""

    def __init__(self, command: str | None, *, repo_path: str) -> None:
        self.command = command
        self.repo_path = repo_path

    def dispatch(self, lifecycle: PRLifecycle, disposition: str) -> dict[str, Any]:
        if not self.command:
            raise LifecycleError("terminal cleanup command is unavailable")
        if len(lifecycle.task_ids) != 1:
            raise LifecycleError(
                f"terminal cleanup requires exactly one owning task id; PR #{lifecycle.number} has {lifecycle.task_ids!r}"
            )
        command = shlex.split(self.command) + [
            "cleanup",
            "--task",
            lifecycle.task_ids[0],
            "--branch",
            lifecycle.branch,
            "--expected-head",
            lifecycle.head,
            "--pr-number",
            str(lifecycle.number),
            "--repo",
            self.repo_path,
            "--disposition",
            disposition,
            "--json",
        ]
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no command output"
            raise LifecycleError(f"terminal cleanup failed with exit {completed.returncode}: {detail}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise LifecycleError("terminal cleanup command did not return JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise LifecycleError(f"terminal cleanup command returned invalid result: {payload!r}")
        return payload
