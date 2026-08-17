"""Bounded Asana subtask I/O for post-merge Review recovery."""
from __future__ import annotations

from typing import Any
from urllib import parse as urlparse

from pr_lifecycle_support import LifecycleError
from pr_lifecycle_post_merge_review_types import PostMergeAsana

def _list_subtasks(asana: PostMergeAsana, gid: str) -> list[dict[str, Any]]:
    method = getattr(asana, "list_subtasks", None)
    if callable(method):
        return [dict(item) for item in method(gid)]
    http = getattr(asana, "http", None)
    api_root = str(getattr(asana, "api_root", ""))
    headers = getattr(asana, "headers", None)
    if http is None or not api_root or headers is None:
        raise LifecycleError("Asana adapter cannot enumerate post-merge Review obligation subtasks")
    values: list[dict[str, Any]] = []
    offset: str | None = None
    seen_offsets: set[str] = set()
    for _ in range(1000):
        params: dict[str, Any] = {
            "opt_fields": "gid,name,notes,completed,parent.gid,permalink_url,modified_at",
            "limit": 100,
        }
        if offset is not None:
            params["offset"] = offset
        _, _, payload = http.request(
            "GET",
            f"{api_root}/tasks/{gid}/subtasks?{urlparse.urlencode(params)}",
            headers=headers,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise LifecycleError(f"Asana task {gid} subtasks response was not a list")
        values.extend(dict(item) for item in payload["data"] if isinstance(item, dict))
        next_page = payload.get("next_page")
        if next_page is None:
            return values
        if not isinstance(next_page, dict) or not isinstance(next_page.get("offset"), str) or not next_page["offset"]:
            raise LifecycleError(f"Asana task {gid} subtasks response had a malformed next_page")
        offset = next_page["offset"]
        if offset in seen_offsets:
            raise LifecycleError(f"Asana task {gid} subtasks pagination repeated offset {offset!r}")
        seen_offsets.add(offset)
    raise LifecycleError(f"Asana task {gid} subtasks pagination exceeded 1000 pages")


def _create_subtask(asana: PostMergeAsana, gid: str, *, name: str, notes: str) -> dict[str, Any]:
    method = getattr(asana, "create_subtask", None)
    if callable(method):
        return dict(method(gid, name=name, notes=notes))
    http = getattr(asana, "http", None)
    api_root = str(getattr(asana, "api_root", ""))
    headers = getattr(asana, "headers", None)
    if http is None or not api_root or headers is None:
        raise LifecycleError("Asana adapter cannot create a post-merge Review obligation subtask")
    query = urlparse.urlencode({
        "opt_fields": "gid,name,notes,completed,parent.gid,permalink_url,modified_at"
    })
    _, _, value = http.request(
        "POST",
        f"{api_root}/tasks/{gid}/subtasks?{query}",
        headers=headers,
        body={"data": {"name": name, "notes": notes}},
    )
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise LifecycleError(f"Asana task {gid} subtask creation response was not an object")
    return dict(value["data"])
