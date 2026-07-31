"""Canonical stateful Asana fake used by Dish workflow tests.

The fake models only the adapter contract Dish relies on: task reads, task creation,
content/completion updates, section movement, and project section listing. Tests may
install deterministic before/after hooks or forbid operations instead of reimplementing
those semantics in local backend classes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from dish_tool.constants import COOKING_PROJECT_GID

DEFAULT_SECTIONS = (
    {"gid": "rq", "name": "Research Queue"},
    {"gid": "vq", "name": "Verification Queue"},
    {"gid": "12345", "name": "Sichuan"},
    {"gid": "pi", "name": "Planning (Incomplete)"},
    {"gid": "src", "name": "Sourcing"},
    {"gid": "ref", "name": "Reference"},
)


@dataclass(frozen=True)
class BackendCall:
    operation: str
    arguments: dict[str, Any]


class StatefulAsanaBackend:
    """In-memory implementation of the Asana operations used by Dish.

    A default task is exposed through the legacy ``title``/``notes``/``section``
    attributes so existing workflow helpers can migrate without changing their test
    assertions. Multi-task tests can seed explicit task dictionaries.
    """

    def __init__(
        self,
        title: str = "Bare",
        notes: str = "",
        section: str = "rq",
        completed: bool = False,
        *,
        task_gid: str = "task",
        project_gid: str = COOKING_PROJECT_GID,
        sections: Iterable[dict[str, str]] | None = None,
        tasks: Iterable[dict[str, Any]] | None = None,
        created_task_gid: str = "1000000000000001",
    ) -> None:
        self.task_gid = task_gid
        self.project_gid = project_gid
        self.created_task_gid = created_task_gid
        self.sections = [dict(item) for item in (sections or DEFAULT_SECTIONS)]
        self._tasks: dict[str, dict[str, Any]] = {}
        self._allow_unknown_task_aliases = tasks is None
        self._calls: list[BackendCall] = []
        self._before: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self._after: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self._forbidden: dict[str, str] = {}

        if tasks is None:
            self.add_task(
                task_gid=task_gid,
                title=title,
                notes=notes,
                section_gid=section,
                completed=completed,
            )
        else:
            for item in tasks:
                self.add_task(
                    task_gid=str(item.get("task_gid", item.get("gid"))),
                    title=str(item["title"]),
                    notes=str(item["notes"]),
                    section_gid=str(item.get("section_gid", item.get("section", section))),
                    completed=bool(item.get("completed", False)),
                    modified_at=str(item.get("modified_at", "fixture")),
                )
            if task_gid not in self._tasks and len(self._tasks) == 1:
                self.task_gid = next(iter(self._tasks))

    def add_task(
        self,
        *,
        task_gid: str,
        title: str,
        notes: str,
        section_gid: str,
        completed: bool = False,
        modified_at: str = "now",
    ) -> None:
        self._tasks[task_gid] = {
            "title": title,
            "notes": notes,
            "section_gid": section_gid,
            "completed": completed,
            "modified_at": modified_at,
        }

    @property
    def title(self) -> str:
        return self._tasks[self.task_gid]["title"]

    @title.setter
    def title(self, value: str) -> None:
        self._tasks[self.task_gid]["title"] = value

    @property
    def notes(self) -> str:
        return self._tasks[self.task_gid]["notes"]

    @notes.setter
    def notes(self, value: str) -> None:
        self._tasks[self.task_gid]["notes"] = value

    @property
    def section(self) -> str:
        return self._tasks[self.task_gid]["section_gid"]

    @section.setter
    def section(self, value: str) -> None:
        self._tasks[self.task_gid]["section_gid"] = value

    @property
    def completed(self) -> bool:
        return self._tasks[self.task_gid]["completed"]

    @completed.setter
    def completed(self, value: bool) -> None:
        self._tasks[self.task_gid]["completed"] = value

    @property
    def writes(self) -> int:
        return sum(
            call.operation in {"create_bare_task", "update_task_content", "update_task_completed"}
            for call in self._calls
        )

    @property
    def moves(self) -> int:
        return len(self.calls("move_task_to_section"))

    @property
    def create_calls(self) -> int:
        return len(self.calls("create_bare_task"))

    @property
    def tasks(self) -> dict[str, dict[str, Any]]:
        return self._tasks

    def calls(self, operation: str | None = None) -> list[BackendCall]:
        if operation is None:
            return list(self._calls)
        return [call for call in self._calls if call.operation == operation]

    def before(self, operation: str, callback: Callable[..., None]) -> None:
        self._before[operation].append(callback)

    def after(self, operation: str, callback: Callable[..., None]) -> None:
        self._after[operation].append(callback)

    def forbid(self, operation: str, message: str | None = None) -> None:
        self._forbidden[operation] = message or f"unexpected Asana operation: {operation}"

    def _invoke(self, operation: str, arguments: dict[str, Any], effect: Callable[[], Any]) -> Any:
        if operation in self._forbidden:
            raise AssertionError(self._forbidden[operation])
        for callback in self._before[operation]:
            callback(**arguments)
        self._calls.append(BackendCall(operation, deepcopy(arguments)))
        result = effect()
        for callback in self._after[operation]:
            callback(result=result, **arguments)
        return result

    def list_sections(self, project_gid: str) -> list[dict[str, str]]:
        arguments = {"project_gid": project_gid}

        def effect() -> list[dict[str, str]]:
            if project_gid != self.project_gid:
                raise AssertionError(f"unexpected project gid: {project_gid}")
            return deepcopy(self.sections)

        return self._invoke("list_sections", arguments, effect)

    def _task(self, gid: str) -> dict[str, Any]:
        if gid not in self._tasks and self._allow_unknown_task_aliases:
            self._tasks[gid] = self._tasks[self.task_gid]
        return self._tasks[gid]

    def read_task(self, gid: str) -> dict[str, Any]:
        arguments = {"gid": gid}

        def effect() -> dict[str, Any]:
            item = self._task(gid)
            return {
                "gid": gid,
                "name": item["title"],
                "notes": item["notes"],
                "completed": item["completed"],
                "modified_at": item["modified_at"],
                "projects": [{"gid": self.project_gid}],
                "memberships": [
                    {
                        "project": {"gid": self.project_gid},
                        "section": {"gid": item["section_gid"]},
                    }
                ],
            }

        return self._invoke("read_task", arguments, effect)

    def create_bare_task(self, *, title: str, project_gid: str, section_gid: str) -> dict[str, Any]:
        arguments = {"title": title, "project_gid": project_gid, "section_gid": section_gid}

        def effect() -> dict[str, Any]:
            if project_gid != self.project_gid:
                raise AssertionError(f"unexpected project gid: {project_gid}")
            gid = self.created_task_gid
            self.add_task(task_gid=gid, title=title, notes="", section_gid=section_gid)
            self.task_gid = gid
            return {"gid": gid, "name": title, "notes": ""}

        return self._invoke("create_bare_task", arguments, effect)

    def update_task_content(self, *, task_gid: str, title: str, notes: str) -> None:
        arguments = {"task_gid": task_gid, "title": title, "notes": notes}

        def effect() -> None:
            self._task(task_gid)["title"] = title
            self._task(task_gid)["notes"] = notes

        self._invoke("update_task_content", arguments, effect)

    def update_task_completed(self, *, task_gid: str, completed: bool) -> None:
        arguments = {"task_gid": task_gid, "completed": completed}

        def effect() -> None:
            self._task(task_gid)["completed"] = completed

        self._invoke("update_task_completed", arguments, effect)

    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None:
        arguments = {"task_gid": task_gid, "section_gid": section_gid}

        def effect() -> None:
            self._task(task_gid)["section_gid"] = section_gid

        self._invoke("move_task_to_section", arguments, effect)
