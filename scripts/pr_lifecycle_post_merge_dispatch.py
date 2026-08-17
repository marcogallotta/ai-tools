"""Workspace-Agent dispatch for exact-head full post-merge Review."""
from __future__ import annotations

import hashlib

from pr_lifecycle_support import *

def _dispatch_full_review(
    workspace: WorkspaceAgentDispatcher,
    *,
    repository: str,
    pr_number: int,
    pr_url: str,
    head: str,
    obligation_key: str,
    obligation_task_gid: str,
    marker: str,
) -> WorkspaceDispatchResult:
    legacy = getattr(workspace, "dispatch_post_merge", None)
    if callable(legacy):
        return legacy(
            repository=repository,
            pr_number=pr_number,
            pr_url=pr_url,
            head=head,
            obligation_key=obligation_key,
            obligation_task_gid=obligation_task_gid,
            full_review_marker=marker,
        )
    trigger_id = workspace.review_trigger_id
    if not workspace.access_token:
        raise LifecycleError("Workspace Agent access token is unavailable")
    if not trigger_id:
        raise LifecycleError("published ChatGPT Review Workspace Agent trigger is unavailable")
    identity = f"dish-post-merge-review:v1:{repository}:{pr_number}:{head}:{obligation_key}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    prompt = (
        "Perform the full post-merge Dish Review required by an explicit Review request. "
        f"Repository: {repository}. PR: {pr_url} (#{pr_number}). "
        f"The PR is already merged; review the exact merged PR head {head}. "
        f"Durable Asana full-review obligation: {obligation_task_gid}. "
        "Later main movement is context, not the candidate identity, and any pre-merge Review does not satisfy "
        "this explicit post-merge obligation. Read and follow dish/docs/agents/review.md. "
        "Do not edit source or treat already-merged state as a no-op. The authoritative completion artifact is a "
        "formal GitHub COMMENT review anchored to the exact head with VERDICT: MERGE or VERDICT: BLOCK. "
        f"Include this exact marker in that formal review body: {marker} "
        "VERDICT: BLOCK must describe the required corrective scope; the existing lifecycle will route a bounded "
        "Implementation owner. Do not claim database/runtime/deployment/external effects are recovered from source Review."
    )
    headers = {
        "Authorization": f"Bearer {workspace.access_token}",
        "OpenAI-Beta": WORKSPACE_RUNS_BETA,
        "Idempotency-Key": key,
        "Content-Type": "application/json",
    }
    _, _, value = workspace.http.request(
        "POST",
        f"{workspace.api_root}/workspace_agents/{trigger_id}/trigger",
        headers=headers,
        body={
            "conversation_key": (
                f"dish-post-merge-review-{repository.replace('/', '-')}-{pr_number}-{head}-{obligation_key}"
            ),
            "input": prompt,
        },
    )
    if not isinstance(value, dict):
        raise LifecycleError("Workspace Agent post-merge trigger response was not an object")
    return WorkspaceDispatchResult(
        idempotency_key=key,
        conversation_url=value.get("conversation_url"),
        run_id=value.get("agent_trigger_run_id"),
    )
