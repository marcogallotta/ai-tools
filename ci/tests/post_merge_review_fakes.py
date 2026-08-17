from __future__ import annotations

from copy import deepcopy
import hashlib
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("pr_lifecycle_post_merge_test", SCRIPTS / "pr_lifecycle.py")
assert SPEC and SPEC.loader
pr_lifecycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_lifecycle)

import pr_lifecycle_post_merge_review as post_merge

HEAD = "a" * 40
OWNER = "1217443403986570"
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def merged_pr(*, head: str = HEAD) -> dict:
    return {
        "number": 31,
        "html_url": "https://github.com/marcogallotta/ai-tools/pull/31",
        "title": "Merged recovery target",
        "state": "closed",
        "draft": False,
        "merged": True,
        "merged_at": NOW.isoformat(),
        "body": f"Owning task: {OWNER}",
        "head": {"sha": head, "ref": "agent/merged-target"},
        "base": {"ref": "main", "sha": "c" * 40},
        "mergeable": None,
        "mergeable_state": "unknown",
    }


def review(*, body: str, head: str = HEAD, review_id: int = 10) -> dict:
    return {
        "id": review_id,
        "state": "COMMENTED",
        "commit_id": head,
        "submitted_at": NOW.isoformat(),
        "body": body,
    }


class FakeGitHub:
    repository = "marcogallotta/ai-tools"

    def __init__(self):
        self.pr = merged_pr()
        self.comments: list[dict] = []
        self.reviews: list[dict] = []

    def get_pr(self, number: int) -> dict:
        assert number == 31
        return deepcopy(self.pr)

    def get_comments(self, number: int) -> list[dict]:
        assert number == 31
        return deepcopy(self.comments)

    def get_reviews(self, number: int) -> list[dict]:
        assert number == 31
        return deepcopy(self.reviews)

    def add_comment(self, number: int, body: str) -> dict:
        assert number == 31
        item = {"id": len(self.comments) + 1, "body": body, "created_at": NOW.isoformat()}
        self.comments.append(item)
        return deepcopy(item)


class FakeAsana:
    def __init__(self):
        self.tasks = {
            OWNER: {
                "gid": OWNER,
                "name": "Owning task",
                "notes": "",
                "completed": True,
                "parent": None,
                "permalink_url": f"https://app.asana.com/0/0/{OWNER}",
            }
        }
        self.children: dict[str, list[str]] = {OWNER: []}
        self.stories: dict[str, list[dict]] = {}
        self.next_gid = 1217990000000000

    def get_task(self, gid: str) -> dict:
        return deepcopy(self.tasks[gid])

    def list_subtasks(self, gid: str) -> list[dict]:
        return [deepcopy(self.tasks[child]) for child in self.children.get(gid, [])]

    def create_subtask(self, gid: str, *, name: str, notes: str) -> dict:
        child = str(self.next_gid)
        self.next_gid += 1
        task = {
            "gid": child,
            "name": name,
            "notes": notes,
            "completed": False,
            "parent": {"gid": gid},
            "permalink_url": f"https://app.asana.com/0/0/{child}",
        }
        self.tasks[child] = task
        self.children.setdefault(gid, []).append(child)
        self.stories.setdefault(child, [])
        return deepcopy(task)

    def get_stories(self, gid: str) -> list[dict]:
        return deepcopy(self.stories.get(gid, []))

    def add_comment(self, gid: str, text: str) -> dict:
        story = {"gid": str(len(self.stories.setdefault(gid, [])) + 1), "text": text}
        self.stories[gid].append(story)
        return deepcopy(story)

    def update_task_fields(self, gid: str, fields: dict) -> dict:
        self.tasks[gid].update(fields)
        return deepcopy(self.tasks[gid])


class FakeWorkspace:
    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def dispatch_post_merge(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if self.fail:
            raise pr_lifecycle.LifecycleError("workspace unavailable")
        identity = (
            f"dish-post-merge-review:v1:{kwargs['repository']}:{kwargs['pr_number']}:"
            f"{kwargs['head']}:{kwargs['obligation_key']}"
        )
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return pr_lifecycle.WorkspaceDispatchResult(key, "https://chatgpt.com/c/review", "run_post_merge")


def engine(gh: FakeGitHub, asana: FakeAsana):
    return pr_lifecycle.LifecycleEngine(gh, asana=asana, now=lambda: NOW)


def obligation(asana: FakeAsana):
    values = [
        post_merge._parse_obligation(item)
        for item in asana.list_subtasks(OWNER)
        if post_merge.OBLIGATION_MARKER in str(item.get("notes") or "")
    ]
    return [item for item in values if item is not None]
