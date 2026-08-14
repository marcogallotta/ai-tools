from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import ClaimError

MARKER_PREFIX = "dish-implementation-claim:v1 "


class AsanaMirrorProtocol(Protocol):
    def sync(self, claim: dict[str, Any]) -> None: ...


class NullAsanaMirror:
    def sync(self, claim: dict[str, Any]) -> None:
        return None


@dataclass(slots=True)
class AsanaMirror:
    token: str
    allowed_projects: frozenset[str]
    api_base: str = "https://app.asana.com/api/1.0"
    active_section: str = "In Progress"
    review_section: str = "Review / Integration"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = self.api_base.rstrip("/") + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ClaimError("ASANA_UNAVAILABLE", f"Asana HTTP {exc.code}: {detail}", 503) from exc
        except OSError as exc:
            raise ClaimError("ASANA_UNAVAILABLE", f"Asana request failed: {exc}", 503) from exc
        return payload.get("data")

    def _task(self, task_gid: str) -> dict[str, Any]:
        fields = urllib.parse.quote(
            "gid,name,completed,memberships.project.gid,memberships.project.name,memberships.section.gid,memberships.section.name"
        )
        data = self._request("GET", f"/tasks/{task_gid}?opt_fields={fields}")
        if not isinstance(data, dict):
            raise ClaimError("ASANA_INVALID", "Asana task response was not an object", 503)
        return data

    def _sections(self, project_gid: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/projects/{project_gid}/sections?opt_fields=gid,name&limit=100")
        if not isinstance(data, list):
            raise ClaimError("ASANA_INVALID", "Asana sections response was not a list", 503)
        return [item for item in data if isinstance(item, dict)]

    def _stories(self, task_gid: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/tasks/{task_gid}/stories?opt_fields=gid,text,created_at&limit=100")
        if not isinstance(data, list):
            raise ClaimError("ASANA_INVALID", "Asana stories response was not a list", 503)
        return [item for item in data if isinstance(item, dict)]

    def _allowed_memberships(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for membership in task.get("memberships") or []:
            project_gid = str((membership.get("project") or {}).get("gid") or "")
            if not self.allowed_projects or project_gid in self.allowed_projects:
                out.append(membership)
        return out

    def _move(self, task: dict[str, Any], target_name: str, *, only_from_ready: bool) -> None:
        for membership in self._allowed_memberships(task):
            section = membership.get("section") or {}
            current_name = section.get("name")
            if current_name == target_name:
                continue
            if only_from_ready and current_name != "Ready":
                continue
            project_gid = str((membership.get("project") or {}).get("gid") or "")
            target = next((s for s in self._sections(project_gid) if s.get("name") == target_name), None)
            if target is None:
                raise ClaimError("ASANA_SECTION_MISSING", f"project {project_gid} has no exact {target_name!r} section", 503)
            self._request("POST", f"/sections/{target['gid']}/addTask", {"data": {"task": task["gid"]}})

    @staticmethod
    def marker(claim: dict[str, Any]) -> str:
        return MARKER_PREFIX + json.dumps(
            {
                "repository": claim["repository"],
                "task_gid": claim["task_gid"],
                "role": claim["role"],
                "generation": claim["generation"],
                "claim_id": claim["claim_id"],
                "owner": claim["owner"],
                "session_id": claim["session_id"],
                "host": claim["host"],
                "authoring_base_sha": claim["authoring_base_sha"],
                "state": claim["state"],
                "branch": claim["branch"],
                "branch_head": claim["branch_head"],
                "pr_number": claim["pr_number"],
                "pr_head": claim["pr_head"],
                "last_event": claim["last_event"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def sync(self, claim: dict[str, Any]) -> None:
        task = self._task(claim["task_gid"])
        if task.get("completed") and claim["state"] in {"claimed", "publishing"}:
            raise ClaimError("TASK_COMPLETED", "Asana task is completed; writable Implementation ownership is refused", 409)
        memberships = self._allowed_memberships(task)
        if self.allowed_projects and not memberships:
            raise ClaimError("TASK_PROJECT_DENIED", "Asana task is outside configured orchestration projects", 403)

        if claim["state"] in {"claimed", "publishing"}:
            self._move(task, self.active_section, only_from_ready=True)
        elif claim["state"] == "review-ready":
            self._move(task, self.review_section, only_from_ready=False)

        marker = self.marker(claim)
        if not any(story.get("text") == marker for story in self._stories(claim["task_gid"])):
            self._request("POST", f"/tasks/{claim['task_gid']}/stories", {"data": {"text": marker}})

        # Authoritative readback: active work may never still be dispatchable as Ready,
        # and the exact generation marker must exist before the store marks it synced.
        readback = self._task(claim["task_gid"])
        if claim["state"] in {"claimed", "publishing", "review-ready"}:
            ready = [
                membership for membership in self._allowed_memberships(readback)
                if (membership.get("section") or {}).get("name") == "Ready"
            ]
            if ready:
                raise ClaimError("ASANA_SYNC_VERIFY_FAILED", "task still appears in Ready after active-claim synchronization", 503)
        if not any(story.get("text") == marker for story in self._stories(claim["task_gid"])):
            raise ClaimError("ASANA_SYNC_VERIFY_FAILED", "exact claim generation marker was not readable after synchronization", 503)
