"""Identity types and markers for durable post-merge Review recovery."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
import secrets
from typing import Any, Mapping, Protocol

from pr_lifecycle_support import FULL_SHA_RE, LifecycleError

OBLIGATION_MARKER = "dish-post-merge-review-obligation:v1"
FULL_REVIEW_MARKER = "dish-post-merge-full-review:v1"
OBLIGATION_CLOSE_MARKER = "dish-post-merge-review-complete:v1"
CORRECTIVE_MARKER = "dish-post-merge-corrective:v1"
CORRECTIVE_REVIEW_MARKER = "dish-post-merge-corrective-review:v1"
PR_LINK_MARKER = "dish-post-merge-review-link:v1"
THIN_RESULTS = {"SAFE ENOUGH", "SERIOUS DEFECT FOUND", "UNABLE TO DETERMINE"}

_OBLIGATION_RE = re.compile(
    rf"<!--\s*{re.escape(OBLIGATION_MARKER)}\s+repo=(?P<repo>\S+)\s+pr=(?P<pr>\d+)\s+"
    r"head=(?P<head>[0-9a-f]{40})\s+key=(?P<key>[0-9a-f]{24})\s*-->",
    re.I,
)
_FULL_REVIEW_RE = re.compile(
    rf"<!--\s*{re.escape(FULL_REVIEW_MARKER)}\s+key=(?P<key>[0-9a-f]{{24}})\s+"
    r"head=(?P<head>[0-9a-f]{40})\s*-->",
    re.I,
)


class PostMergeAsana(Protocol):
    def get_task(self, gid: str) -> dict[str, Any]: ...
    def get_stories(self, gid: str) -> list[dict[str, Any]]: ...
    def add_comment(self, gid: str, text: str) -> dict[str, Any]: ...
    def update_task_fields(self, gid: str, fields: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PostMergeReviewObligation:
    repository: str
    pr_number: int
    head: str
    key: str
    owner_task_gid: str
    task_gid: str
    thin_result: str | None
    completed: bool
    permalink_url: str | None = None

    def json(self) -> dict[str, Any]:
        return asdict(self)


def _validate_identity(repository: str, pr_number: int, head: str) -> tuple[str, int, str]:
    if FULL_SHA_RE.fullmatch(head) is None:
        raise LifecycleError("post-merge Review obligation requires an exact 40-character PR head SHA")
    return repository, int(pr_number), head.lower()


def obligation_key(repository: str, pr_number: int, head: str) -> str:
    repository, pr_number, head = _validate_identity(repository, pr_number, head)
    identity = f"{repository}:{pr_number}:{head}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def new_obligation_key(repository: str, pr_number: int, head: str) -> str:
    repository, pr_number, head = _validate_identity(repository, pr_number, head)
    nonce = secrets.token_hex(16)
    identity = f"{repository}:{pr_number}:{head}:round:{nonce}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def obligation_marker(*, repository: str, pr_number: int, head: str, key: str) -> str:
    return f"<!-- {OBLIGATION_MARKER} repo={repository} pr={pr_number} head={head} key={key} -->"


def full_review_marker(*, key: str, head: str) -> str:
    return f"<!-- {FULL_REVIEW_MARKER} key={key} head={head} -->"


def pr_link_marker(*, obligation: PostMergeReviewObligation) -> str:
    return (
        f"<!-- {PR_LINK_MARKER} key={obligation.key} task={obligation.task_gid} "
        f"head={obligation.head} -->"
    )
