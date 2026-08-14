from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import ClaimError

DEFAULT_REPOSITORY = "marcogallotta/ai-tools"


@dataclass(slots=True)
class ClaimServiceClient:
    url: str
    token: str
    repository: str = DEFAULT_REPOSITORY
    recovery_token: str | None = None

    @classmethod
    def from_env(cls) -> "ClaimServiceClient":
        url = os.environ.get("DISH_IMPLEMENTATION_CLAIM_URL", "").rstrip("/")
        token = os.environ.get("DISH_IMPLEMENTATION_CLAIM_TOKEN", "")
        if not url or not token:
            raise ClaimError(
                "GLOBAL_CLAIM_UNAVAILABLE",
                "DISH_IMPLEMENTATION_CLAIM_URL and DISH_IMPLEMENTATION_CLAIM_TOKEN are required for writable Implementation work",
                503,
            )
        if not url.startswith("https://") and not (
            os.environ.get("DISH_IMPLEMENTATION_CLAIM_ALLOW_HTTP") == "1" and url.startswith("http://")
        ):
            raise ClaimError("GLOBAL_CLAIM_TRANSPORT", "claim service URL must use HTTPS", 503)
        return cls(
            url=url,
            token=token,
            repository=os.environ.get("DISH_IMPLEMENTATION_CLAIM_REPOSITORY", DEFAULT_REPOSITORY),
            recovery_token=os.environ.get("DISH_IMPLEMENTATION_CLAIM_RECOVERY_TOKEN") or None,
        )

    def _request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, str] | None = None,
        writer_capability: str | None = None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        url = self.url + "/v1/claim"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if writer_capability is not None:
            headers["X-Dish-Writer-Capability"] = writer_capability
        if recovery:
            if not self.recovery_token:
                raise ClaimError(
                    "RECOVERY_AUTHORITY_REQUIRED",
                    "DISH_IMPLEMENTATION_CLAIM_RECOVERY_TOKEN is required for takeover",
                    403,
                )
            headers["X-Dish-Recovery-Authorization"] = f"Bearer {self.recovery_token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {"error": {"code": "SERVICE_UNAVAILABLE", "message": f"claim service HTTP {exc.code}"}}
            error = body.get("error") or {}
            raise ClaimError(
                str(error.get("code") or "SERVICE_UNAVAILABLE"),
                str(error.get("message") or "claim service failed"),
                exc.code,
                current=body.get("current"),
                writer_capability=body.get("writer_capability"),
            ) from exc
        except OSError as exc:
            raise ClaimError("SERVICE_UNAVAILABLE", f"claim service request failed: {exc}", 503) from exc
        if not body.get("ok"):
            error = body.get("error") or {}
            raise ClaimError(
                str(error.get("code") or "SERVICE_UNAVAILABLE"),
                str(error.get("message") or "claim service failed"),
                503,
                current=body.get("current"),
                writer_capability=body.get("writer_capability"),
            )
        return body

    def status(self, task_gid: str) -> dict[str, Any] | None:
        return self._request("GET", query={"task_gid": task_gid}).get("claim")

    def dispatch_guard(self, task_gid: str) -> dict[str, Any]:
        return self._request("POST", {"action": "dispatch-guard", "repository": self.repository, "task_gid": task_gid})

    def acquire(self, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "acquire", "repository": self.repository, **kwargs})["claim"]

    def takeover(self, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "takeover", "repository": self.repository, **kwargs}, recovery=True)["claim"]

    def sync(self, *, task_gid: str, claim_id: str, writer_capability: str) -> dict[str, Any]:
        return self._request(
            "POST",
            {"action": "sync", "repository": self.repository, "task_gid": task_gid, "claim_id": claim_id},
            writer_capability=writer_capability,
        )["claim"]

    def authorize(self, *, task_gid: str, claim_id: str, writer_capability: str, branch: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": "authorize", "repository": self.repository, "task_gid": task_gid, "claim_id": claim_id}
        if branch is not None:
            payload["branch"] = branch
        return self._request("POST", payload, writer_capability=writer_capability)["claim"]

    def renew(self, *, task_gid: str, claim_id: str, writer_capability: str) -> dict[str, Any]:
        return self._request(
            "POST",
            {"action": "renew", "repository": self.repository, "task_gid": task_gid, "claim_id": claim_id},
            writer_capability=writer_capability,
        )["claim"]

    def bind_branch(self, *, task_gid: str, claim_id: str, writer_capability: str, branch: str) -> dict[str, Any]:
        return self._request(
            "POST",
            {"action": "bind-branch", "repository": self.repository, "task_gid": task_gid, "claim_id": claim_id, "branch": branch},
            writer_capability=writer_capability,
        )["claim"]

    def bind_pr(self, *, task_gid: str, claim_id: str, writer_capability: str, pr_number: int, pr_head: str) -> dict[str, Any]:
        return self._request(
            "POST",
            {"action": "bind-pr", "repository": self.repository, "task_gid": task_gid, "claim_id": claim_id, "pr_number": pr_number, "pr_head": pr_head},
            writer_capability=writer_capability,
        )["claim"]

    def begin_publication(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "begin-publication", "repository": self.repository, **kwargs}, writer_capability=writer_capability)

    def complete_publication(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "complete-publication", "repository": self.repository, **kwargs}, writer_capability=writer_capability)

    def abort_publication(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "abort-publication", "repository": self.repository, **kwargs}, writer_capability=writer_capability)["claim"]

    def reconcile_publication(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "reconcile-publication", "repository": self.repository, **kwargs}, writer_capability=writer_capability)

    def review_ready(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "review-ready", "repository": self.repository, **kwargs}, writer_capability=writer_capability)["claim"]

    def release(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "release", "repository": self.repository, **kwargs}, writer_capability=writer_capability)["claim"]

    def supersede(self, *, writer_capability: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("POST", {"action": "supersede", "repository": self.repository, **kwargs}, writer_capability=writer_capability)["claim"]
