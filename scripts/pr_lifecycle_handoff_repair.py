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
OWNER_CONSUMER_ROUTE = "WorkspaceAgentDispatcher.dispatch_worker"
REQUIRED_READBACK = (
    "the same repository/PR/branch/head packet is accepted by the Development Workflow consumer, "
    "then repaired metadata survives authoritative GitHub readback"
)


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


def repair_packet(
    *, repository: str, pr: Mapping[str, Any], repair: HandoffRepair
) -> dict[str, Any]:
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    return {
        "schema": "dish-handoff-repair-dispatch-v1",
        "repository": repository,
        "pr_number": int(pr["number"]),
        "pr_url": str(pr.get("html_url") or pr.get("url") or ""),
        "branch": str(head.get("ref") or ""),
        "head": str(head.get("sha") or "").lower(),
        "owning_task": repair.task_gid,
        "defect": repair.defect,
        "repair_owner": repair.repair_owner,
        "next_action": repair.next_repair_action,
        "identity_basis": repair.identity_basis,
        "required_readback": REQUIRED_READBACK,
    }


def capability_blocker(
    *, packet: Mapping[str, Any], missing_route: str, detail: str
) -> dict[str, Any]:
    return {
        "missing_route": missing_route,
        "development_workflow_owner": "Development Workflow / orchestration",
        "detail": detail,
        "recovery_evidence": (
            "the missing route accepts this exact packet and the durable OWNER_CONSUMER_ACCEPTED "
            "marker reads back on the unchanged head"
        ),
        "packet": dict(packet),
    }


def repair_comment(
    head: str,
    repair: HandoffRepair,
    *,
    readback_status: str,
    packet: Mapping[str, Any] | None = None,
    blocker: Mapping[str, Any] | None = None,
) -> str:
    payload = repair.json()
    payload.update(head=head, readback_status=readback_status, key=repair_key(head, repair))
    if packet is not None:
        payload["packet"] = dict(packet)
    if blocker is not None:
        payload["capability_blocker"] = dict(blocker)
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


def repair_delivery_present(
    comments: list[Mapping[str, Any]], *, head: str, repair: HandoffRepair
) -> bool:
    key = repair_key(head, repair)
    return any(
        REPAIR_MARKER in str(item.get("body") or "")
        and f'"key":"{key}"' in str(item.get("body") or "")
        and '"readback_status":"OWNER_CONSUMER_ACCEPTED"' in str(item.get("body") or "")
        for item in comments
    )


def repair_blocker_present(
    comments: list[Mapping[str, Any]], *, head: str, repair: HandoffRepair
) -> bool:
    key = repair_key(head, repair)
    return any(
        REPAIR_MARKER in str(item.get("body") or "")
        and f'"key":"{key}"' in str(item.get("body") or "")
        and '"readback_status":"CAPABILITY_BLOCKED"' in str(item.get("body") or "")
        for item in comments
    )


def repaired_body(pr: Mapping[str, Any], task_gid: str) -> str:
    marker = canonical_owning_task_marker(task_gid)
    body = str(pr.get("body") or "")
    return marker if not body else marker + "\n" + body
