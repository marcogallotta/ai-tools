#!/usr/bin/env python3
"""Safely apply a mailbox series to a dedicated integration worktree.

The mutation boundary binds repository/worktree/branch/HEAD/main identity and
routes every resulting commit through tools/git-commit. It intentionally does
not use ``git am`` because ``git am`` creates commits itself and would bypass
the project commit mechanism.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# These variables can redirect Git away from the worktree/repository selected
# by -C, swap its index/ref namespace, or otherwise make the resolved mutation
# target differ from the path the operator supplied. Fail closed instead of
# silently sanitising them so the caller sees why the requested boundary could
# not be established.
FORBIDDEN_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)


class IntegrationError(RuntimeError):
    pass


def fail(message: str) -> "NoReturn":
    raise IntegrationError(message)


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin=None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=stdin,
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.rstrip() if text else completed.stderr.decode(errors="replace").rstrip()
        fail(f"command failed ({completed.returncode}): {' '.join(argv)}\n{stderr}")
    return completed


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(cwd), *args], check=check)


def absolute_git_path(cwd: Path, *args: str) -> Path:
    raw = git(cwd, "rev-parse", "--path-format=absolute", *args).stdout.strip()
    return Path(raw).resolve()


def current_branch(cwd: Path) -> str:
    result = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0:
        fail("integration worktree must have a branch checked out; detached HEAD is not supported")
    return result.stdout.strip()


def resolve_ref(cwd: Path, ref: str) -> str:
    return git(cwd, "rev-parse", "--verify", ref).stdout.strip()


def parse_worktrees(cwd: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in git(cwd, "worktree", "list", "--porcelain").stdout.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def worktree_record(cwd: Path, worktree: Path) -> dict[str, str]:
    target = worktree.resolve()
    for record in parse_worktrees(cwd):
        if "worktree" in record and Path(record["worktree"]).resolve() == target:
            return record
    fail(f"intended worktree is not registered with this repository: {target}")


def reject_repository_overrides() -> None:
    present = [name for name in FORBIDDEN_GIT_ENV if name in os.environ]
    if present:
        fail(
            "repository-resolution Git environment override(s) are not allowed for mailbox integration: "
            + ", ".join(present)
            + ". Unset them and retry."
        )


def clean_worktree_required(cwd: Path) -> None:
    status = git(cwd, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status:
        fail("integration worktree/index must be clean before mailbox mutation")


class Boundary:
    def __init__(self, execution_cwd: Path, intended_worktree: Path, intended_branch: str):
        self.execution_cwd = execution_cwd.resolve()
        self.worktree = intended_worktree.resolve()
        self.branch = intended_branch

        if self.branch == "main":
            fail("mailbox integration refuses refs/heads/main; candidate work must use a dedicated integration branch")

        intended_top = absolute_git_path(self.worktree, "--show-toplevel")
        if intended_top != self.worktree:
            fail(f"--worktree must name the worktree root exactly: expected {intended_top}")

        resolved_top = absolute_git_path(self.execution_cwd, "--show-toplevel")
        if resolved_top != self.worktree:
            fail(
                f"-C resolved to the wrong worktree: resolved {resolved_top}, intended {self.worktree}"
            )

        self.git_dir = absolute_git_path(self.execution_cwd, "--git-dir")
        intended_git_dir = absolute_git_path(self.worktree, "--git-dir")
        if self.git_dir != intended_git_dir:
            fail(f"resolved git-dir mismatch: {self.git_dir} != {intended_git_dir}")

        self.common_dir = absolute_git_path(self.execution_cwd, "--git-common-dir")
        intended_common_dir = absolute_git_path(self.worktree, "--git-common-dir")
        if self.common_dir != intended_common_dir:
            fail(f"resolved git-common-dir mismatch: {self.common_dir} != {intended_common_dir}")

        actual_branch = current_branch(self.execution_cwd)
        if actual_branch != self.branch:
            fail(f"wrong branch/worktree identity: expected {self.branch!r}, found {actual_branch!r}")

        self.head = resolve_ref(self.execution_cwd, "HEAD")
        self.main = resolve_ref(self.execution_cwd, "refs/heads/main")
        self._verify_worktree_registration(self.head)

    def _verify_worktree_registration(self, expected_head: str) -> None:
        record = worktree_record(self.execution_cwd, self.worktree)
        record_branch = record.get("branch")
        expected_branch = f"refs/heads/{self.branch}"
        if record_branch != expected_branch:
            fail(
                "wrong branch/worktree identity in git worktree registry: "
                f"expected {expected_branch}, found {record_branch or '<detached>'}"
            )
        if record.get("HEAD") != expected_head:
            fail(
                "candidate HEAD changed in git worktree registry: "
                f"expected {expected_head}, found {record.get('HEAD', '<missing>')}"
            )

    def verify(self, expected_head: str) -> None:
        resolved_top = absolute_git_path(self.execution_cwd, "--show-toplevel")
        if resolved_top != self.worktree:
            fail(f"resolved worktree moved during mutation: {resolved_top} != {self.worktree}")
        if absolute_git_path(self.execution_cwd, "--git-dir") != self.git_dir:
            fail("resolved git-dir changed during mutation")
        if absolute_git_path(self.execution_cwd, "--git-common-dir") != self.common_dir:
            fail("resolved git-common-dir changed during mutation")
        actual_branch = current_branch(self.execution_cwd)
        if actual_branch != self.branch:
            fail(f"branch changed during mutation: expected {self.branch!r}, found {actual_branch!r}")
        actual_head = resolve_ref(self.execution_cwd, "HEAD")
        if actual_head != expected_head:
            fail(f"candidate HEAD moved unexpectedly: expected {expected_head}, found {actual_head}")
        actual_main = resolve_ref(self.execution_cwd, "refs/heads/main")
        if actual_main != self.main:
            fail(f"refs/heads/main moved unexpectedly: expected {self.main}, found {actual_main}")
        self._verify_worktree_registration(expected_head)

    def verify_main(self) -> None:
        actual_main = resolve_ref(self.execution_cwd, "refs/heads/main")
        if actual_main != self.main:
            fail(f"refs/heads/main moved unexpectedly: expected {self.main}, found {actual_main}")


def parse_mailinfo(worktree: Path, mail: Path, temp: Path) -> tuple[str, str, str, Path]:
    message_path = temp / f"{mail.name}.msg"
    patch_path = temp / f"{mail.name}.diff"
    with mail.open("rb") as source:
        completed = run(
            [
                "git",
                "-C",
                str(worktree),
                "mailinfo",
                "--encoding=UTF-8",
                str(message_path),
                str(patch_path),
            ],
            stdin=source,
            text=False,
        )
    metadata: dict[str, str] = {}
    for raw in completed.stdout.decode("utf-8", errors="replace").splitlines():
        key, sep, value = raw.partition(": ")
        if sep and key in {"Author", "Email", "Subject", "Date"}:
            metadata[key] = value
    missing = [key for key in ("Author", "Email", "Subject", "Date") if not metadata.get(key)]
    if missing:
        fail(f"mailbox message {mail.name} is missing mailinfo metadata: {', '.join(missing)}")
    body = message_path.read_text(encoding="utf-8").strip()
    message = metadata["Subject"]
    if body:
        message += "\n\n" + body
    author = f"{metadata['Author']} <{metadata['Email']}>"
    return message, author, metadata["Date"], patch_path


def staged_paths(worktree: Path) -> list[str]:
    raw = git(
        worktree,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMRD",
        "-z",
    ).stdout
    return [path for path in raw.split("\0") if path]


def apply_series(boundary: Boundary, mailboxes: list[Path]) -> int:
    commit_tool = boundary.worktree / "tools" / "git-commit"
    if not commit_tool.is_file() or not os.access(commit_tool, os.X_OK):
        fail(f"approved commit mechanism is unavailable or not executable: {commit_tool}")

    clean_worktree_required(boundary.worktree)
    expected_head = boundary.head
    boundary.verify(expected_head)

    with tempfile.TemporaryDirectory(prefix="git-mailbox-integrate-") as temp_name:
        temp = Path(temp_name)
        split = temp / "mail"
        split.mkdir()
        split_result = git(
            boundary.worktree,
            "mailsplit",
            "-d4",
            f"-o{split}",
            "--",
            *(str(path) for path in mailboxes),
        )
        try:
            count = int(split_result.stdout.strip())
        except ValueError:
            fail(f"could not determine mailbox message count: {split_result.stdout!r}")
        if count < 1:
            fail("mailbox contained no messages")

        for number in range(1, count + 1):
            boundary.verify(expected_head)
            mail = split / f"{number:04d}"
            message, author, author_date, patch = parse_mailinfo(boundary.worktree, mail, temp)
            boundary.verify(expected_head)

            applied = git(
                boundary.worktree,
                "apply",
                "--index",
                "--whitespace=nowarn",
                str(patch),
                check=False,
            )
            if applied.returncode != 0:
                boundary.verify(expected_head)
                fail(
                    f"mailbox patch {number}/{count} failed to apply; committed prefix is preserved, "
                    "failed patch was not committed:\n"
                    + applied.stderr.rstrip()
                )

            boundary.verify(expected_head)
            paths = staged_paths(boundary.worktree)
            if not paths:
                fail(f"mailbox patch {number}/{count} produced no staged changes")

            boundary.verify(expected_head)
            env = os.environ.copy()
            env["GIT_AUTHOR_NAME"] = author.rsplit(" <", 1)[0]
            env["GIT_AUTHOR_EMAIL"] = author.rsplit("<", 1)[1].rstrip(">")
            env["GIT_AUTHOR_DATE"] = author_date
            committed = run(
                [
                    str(commit_tool),
                    *paths,
                    "-m",
                    message,
                    "-C",
                    str(boundary.worktree),
                ],
                cwd=boundary.worktree,
                env=env,
                check=False,
            )
            if committed.returncode != 0:
                boundary.verify(expected_head)
                fail(
                    f"approved commit mechanism failed for mailbox patch {number}/{count}:\n"
                    + committed.stderr.rstrip()
                )

            new_head = resolve_ref(boundary.worktree, "HEAD")
            if new_head == expected_head:
                fail(f"approved commit mechanism reported success but HEAD did not advance for patch {number}/{count}")
            # The new commit must be a direct child of the head we verified before
            # invoking the commit mechanism; this catches unexpected candidate
            # movement during the commit window.
            parent = git(boundary.worktree, "rev-parse", f"{new_head}^").stdout.strip()
            if parent != expected_head:
                fail(
                    f"candidate history changed unexpectedly during commit: expected parent {expected_head}, "
                    f"found {parent}"
                )
            expected_head = new_head
            boundary.verify(expected_head)

    boundary.verify(expected_head)
    print(
        "Mailbox integration succeeded: "
        f"branch={boundary.branch} start={boundary.head} head={expected_head} main={boundary.main}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-mailbox-integrate",
        description="Apply mailbox commits inside a fail-closed integration worktree boundary.",
    )
    parser.add_argument("--worktree", required=True, help="exact root of the intended integration worktree")
    parser.add_argument("--branch", required=True, help="intended integration branch (main is refused)")
    parser.add_argument("-C", dest="execution_cwd", help="execution directory; must resolve to --worktree")
    parser.add_argument("mailbox", nargs="+", help="mailbox/format-patch file(s), resolved from the caller's PWD")
    return parser


def main(argv: list[str]) -> int:
    try:
        reject_repository_overrides()
        args = build_parser().parse_args(argv)
        worktree = Path(args.worktree).resolve()
        execution_cwd = Path(args.execution_cwd).resolve() if args.execution_cwd else worktree
        mailboxes = [Path(path).resolve() for path in args.mailbox]
        missing = [str(path) for path in mailboxes if not path.is_file()]
        if missing:
            fail("mailbox file(s) not found: " + ", ".join(missing))
        if shutil.which("git") is None:
            fail("git executable not found on PATH")
        boundary = Boundary(execution_cwd, worktree, args.branch)
        return apply_series(boundary, mailboxes)
    except IntegrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
