"""Bounded malformed-handoff repair on the existing PR lifecycle surface."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from typing import Any, Mapping

from pr_lifecycle_owner import (
    canonical_owning_task_marker,
    canonical_owning_task_markers,
    owning_task_identity_from_pr,
)

REPAIR_MARKER = "dish-handoff-repair:v1"
AUTO_REPAIR = "AUTO_REPAIR"
ROUTE_TO_OWNER = "ROUTE_TO_OWNER"


@dataclass(frozen=True)
class HandoffRepair:
    repair_disposition: str
    repair_owner: str
    human_action_required: bool
    next_repair_action: str
    identity_basis: str
    task_gid: str | None
    defect: str

    def json(self) -> dict[str, Any]:
        return asdict(self)


def classify_handoff_repair(pr: Mapping[str, Any]) -> HandoffRepair | None:
    owner, error = owning_task_identity_from_pr(pr)
    markers = canonical_owning_task_markers(pr)
    if owner is not None and error is None and markers == [owner]:
        return None
    if owner is not None and error is None and not markers:
        # Current producer/finalizer drafts should be normalized to the canonical
        # marker before Review handoff. Already-reviewable legacy PRs with an
        # unambiguous explicit owning-task declaration remain valid inputs;
        # treating marker absence alone as a post-handoff defect would suppress
        # ordinary Review/local-cert dispatch for pre-marker lineages.
        if not bool(pr.get("draft")):
            return None
        return HandoffRepair(
            repair_disposition=AUTO_REPAIR,
            repair_owner="producer/finalizer",
            human_action_required=False,
            next_repair_action="persist the canonical dish-owning-task marker and verify exact-head readback",
            identity_basis="existing explicit owning-task declaration",
            task_gid=owner,
            defect="canonical owning-task marker is missing",
        )
    return HandoffRepair(
        repair_disposition=ROUTE_TO_OWNER,
        repair_owner="Development Workflow / orchestration",
        human_action_required=False,
        next_repair_action=(
            "recover the exact assignment identity from canonical handoff/worktree/Worker authority; "
            "repair the producer-owned handoff and read it back; never guess a task identity"
        ),
        identity_basis="unresolved; authoritative assignment recovery required",
        task_gid=None,
        defect=error or "canonical owning-task metadata is malformed",
    )


def repair_key(head: str, repair: HandoffRepair) -> str:
    raw = "\0".join((head, repair.repair_disposition, repair.repair_owner, repair.defect))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def repair_comment(head: str, repair: HandoffRepair, *, readback_status: str) -> str:
    payload = repair.json()
    payload.update(head=head, readback_status=readback_status, key=repair_key(head, repair))
    return (
        f"<!-- {REPAIR_MARKER} {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->\n"
        "HANDOFF REPAIR — agent/system owned; no Marco relay required.\n\n"
        "— Dish PR lifecycle dispatcher"
    )


def repair_comment_present(comments: list[Mapping[str, Any]], *, head: str, repair: HandoffRepair) -> bool:
    key = repair_key(head, repair)
    return any(
        REPAIR_MARKER in str(item.get("body") or "") and f'"key":"{key}"' in str(item.get("body") or "")
        for item in comments
    )


def repaired_body(pr: Mapping[str, Any], task_gid: str) -> str:
    marker = canonical_owning_task_marker(task_gid)
    body = str(pr.get("body") or "")
    return marker if not body else marker + "\n" + body
