from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .errors import ClaimError


class GitHubReaderProtocol(Protocol):
    def branch_head(self, repository: str, branch: str) -> str | None: ...


@dataclass(slots=True)
class GitHubReader:
    token: str
    api_base: str = "https://api.github.com"

    def branch_head(self, repository: str, branch: str) -> str | None:
        encoded = urllib.parse.quote(branch, safe="")
        req = urllib.request.Request(
            self.api_base.rstrip("/") + f"/repos/{repository}/git/ref/heads/{encoded}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ClaimError("GITHUB_UNAVAILABLE", f"GitHub HTTP {exc.code}: {detail}", 503) from exc
        except OSError as exc:
            raise ClaimError("GITHUB_UNAVAILABLE", f"GitHub request failed: {exc}", 503) from exc
        sha = ((payload.get("object") or {}).get("sha")) if isinstance(payload, dict) else None
        if not isinstance(sha, str) or len(sha) != 40:
            raise ClaimError("GITHUB_INVALID", "GitHub ref response did not contain a full head SHA", 503)
        return sha
