from __future__ import annotations

from collections.abc import Mapping

import pytest

from dish_tool.backend import AsanaBackend
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.errors import BackendFailure, DishRuleError
from dish_tool.models import RequestPhase
from dish_tool.task_store import read_complete_task


OTHER_PROJECT = "other-project"


def _membership(project_gid: str, section_gid: str | None):
    return {
        "project": {"gid": project_gid},
        "section": None if section_gid is None else {"gid": section_gid},
    }


def _task(
    *,
    gid: str = "t",
    name: str = "Dish",
    notes: str = "Notes",
    memberships=None,
    projects=None,
):
    return {
        "gid": gid,
        "name": name,
        "notes": notes,
        "completed": False,
        "modified_at": "now",
        "projects": (
            [{"gid": COOKING_PROJECT_GID}]
            if projects is None
            else projects
        ),
        "memberships": (
            [_membership(COOKING_PROJECT_GID, "rq")]
            if memberships is None
            else memberships
        ),
    }


class StaticTaskBackend:
    def __init__(self, response: Mapping[str, object]):
        self.response = dict(response)

    def read_task(self, task_gid: str):
        return dict(self.response)


@pytest.mark.parametrize(
    "memberships",
    [
        [
            _membership(OTHER_PROJECT, "other"),
            _membership(COOKING_PROJECT_GID, "rq"),
        ],
        [
            _membership(COOKING_PROJECT_GID, "rq"),
            _membership(OTHER_PROJECT, "other"),
        ],
        [
            _membership("other-one", "one"),
            _membership(COOKING_PROJECT_GID, "rq"),
            _membership("other-two", "two"),
        ],
    ],
)
def test_task_reader_selects_only_the_cooking_membership(memberships):
    live = read_complete_task(
        StaticTaskBackend(_task(memberships=memberships)),
        task_gid="t",
        project_gid=COOKING_PROJECT_GID,
    )

    assert live.section_gid == "rq"


def test_task_reader_rejects_distinct_cooking_memberships():
    backend = StaticTaskBackend(
        _task(
            memberships=[
                _membership(COOKING_PROJECT_GID, "rq"),
                _membership(COOKING_PROJECT_GID, "vq"),
            ]
        )
    )

    with pytest.raises(DishRuleError) as caught:
        read_complete_task(
            backend, task_gid="t", project_gid=COOKING_PROJECT_GID
        )

    assert caught.value.rule == "task_membership_ambiguous"


def test_task_reader_accepts_project_membership_without_a_section():
    live = read_complete_task(
        StaticTaskBackend(
            _task(
                memberships=[_membership(OTHER_PROJECT, "other")],
                projects=[
                    {"gid": OTHER_PROJECT},
                    {"gid": COOKING_PROJECT_GID},
                ],
            )
        ),
        task_gid="t",
        project_gid=COOKING_PROJECT_GID,
    )

    assert live.section_gid is None


@pytest.mark.parametrize(
    "memberships",
    [
        [None, _membership(COOKING_PROJECT_GID, "rq")],
        [{"section": {"gid": "rq"}}],
        [{"project": {"gid": COOKING_PROJECT_GID}, "section": "rq"}],
    ],
)
def test_task_reader_rejects_malformed_membership_entries(memberships):
    with pytest.raises(DishRuleError) as caught:
        read_complete_task(
            StaticTaskBackend(_task(memberships=memberships)),
            task_gid="t",
            project_gid=COOKING_PROJECT_GID,
        )

    assert caught.value.rule == "task_membership_malformed"


def test_task_reader_rejects_wrong_backend_task_identity():
    with pytest.raises(DishRuleError) as caught:
        read_complete_task(
            StaticTaskBackend(_task(gid="wrong")),
            task_gid="t",
            project_gid=COOKING_PROJECT_GID,
        )

    assert caught.value.rule == "backend_response_malformed"


def _movement_backend(monkeypatch, before, after):
    backend = AsanaBackend(api_client=object())
    reads = iter([before, after])
    calls = []
    monkeypatch.setattr(backend, "read_task", lambda task_gid: next(reads))
    monkeypatch.setattr(
        backend,
        "call",
        lambda function, *args, **kwargs: calls.append((function.__name__, args)) or {},
    )
    return backend, calls


def _assert_uncertain_effect(caught, *, partial_application: str):
    assert caught.value.code == "BACKEND_UNCERTAIN"
    assert caught.value.retryable is False
    assert caught.value.phase == RequestPhase.RESPONSE_RECEIVED.value
    assert caught.value.details["partial_application"] == partial_application


def test_move_rejects_wrong_identity_before_sending(monkeypatch):
    backend, calls = _movement_backend(
        monkeypatch,
        _task(gid="wrong"),
        _task(memberships=[_membership(COOKING_PROJECT_GID, "vq")]),
    )

    with pytest.raises(BackendFailure) as caught:
        backend.move_task_to_section(task_gid="t", section_gid="vq")

    assert caught.value.code == "BACKEND_REJECTED"
    assert caught.value.retryable is False
    assert caught.value.phase == RequestPhase.RESPONSE_RECEIVED.value
    assert caught.value.details == {
        "expected_task_gid": "t",
        "actual_task_gid": "wrong",
    }
    assert calls == []


def test_move_rejects_wrong_identity_after_sending(monkeypatch):
    backend, _ = _movement_backend(
        monkeypatch,
        _task(),
        _task(gid="wrong", memberships=[_membership(COOKING_PROJECT_GID, "vq")]),
    )

    with pytest.raises(BackendFailure) as caught:
        backend.move_task_to_section(task_gid="t", section_gid="vq")

    _assert_uncertain_effect(caught, partial_application="section_move_requested")
    assert caught.value.details["actual_task_gid"] == "wrong"


@pytest.mark.parametrize(
    "after",
    [
        pytest.param(_task(), id="old-cooking-section"),
        pytest.param(
            _task(
                memberships=[
                    _membership(OTHER_PROJECT, "vq"),
                    _membership(COOKING_PROJECT_GID, "rq"),
                ]
            ),
            id="unrelated-project-section",
        ),
    ],
)
def test_move_requires_requested_cooking_section_on_reread(monkeypatch, after):
    backend, _ = _movement_backend(monkeypatch, _task(), after)

    with pytest.raises(BackendFailure) as caught:
        backend.move_task_to_section(task_gid="t", section_gid="vq")

    _assert_uncertain_effect(caught, partial_application="section_move_requested")
    assert caught.value.details["expected_section_gid"] == "vq"


def test_move_rejects_ambiguous_cooking_placement(monkeypatch):
    backend, _ = _movement_backend(
        monkeypatch,
        _task(),
        _task(
            memberships=[
                _membership(COOKING_PROJECT_GID, "vq"),
                _membership(COOKING_PROJECT_GID, "rq"),
            ]
        ),
    )

    with pytest.raises(BackendFailure) as caught:
        backend.move_task_to_section(task_gid="t", section_gid="vq")

    _assert_uncertain_effect(caught, partial_application="section_move_requested")
    assert caught.value.details["section_gids"] == ["rq", "vq"]


@pytest.mark.parametrize(
    ("after", "changed_field"),
    [
        pytest.param(_task(name="Changed", memberships=[_membership(COOKING_PROJECT_GID, "vq")]), "name", id="title"),
        pytest.param(_task(notes="Changed", memberships=[_membership(COOKING_PROJECT_GID, "vq")]), "notes", id="notes"),
    ],
)
def test_move_rejects_concurrent_content_change(monkeypatch, after, changed_field):
    backend, _ = _movement_backend(monkeypatch, _task(), after)

    with pytest.raises(BackendFailure) as caught:
        backend.move_task_to_section(task_gid="t", section_gid="vq")

    _assert_uncertain_effect(caught, partial_application="section_move_requested")
    assert "content changed" in str(caught.value)
    assert after[changed_field] != _task()[changed_field]


def _create_backend(monkeypatch, confirmed):
    backend = AsanaBackend(api_client=object())
    responses = iter([
        {"gid": "new", "name": "Created", "notes": ""},
        {},
    ])
    monkeypatch.setattr(
        backend,
        "call",
        lambda function, *args, **kwargs: next(responses),
    )
    monkeypatch.setattr(backend, "read_task", lambda task_gid: confirmed)
    return backend


def test_create_rejects_task_outside_research_queue(monkeypatch):
    backend = _create_backend(monkeypatch, _task(gid="new", name="Created"))

    with pytest.raises(BackendFailure) as caught:
        backend.create_bare_task(
            title="Created", project_gid=COOKING_PROJECT_GID, section_gid="vq"
        )

    _assert_uncertain_effect(caught, partial_application="task_created")
    assert caught.value.details["expected_section_gid"] == "vq"


def test_create_rejects_wrong_confirmed_task_identity(monkeypatch):
    backend = _create_backend(
        monkeypatch,
        _task(
            gid="wrong",
            name="Created",
            memberships=[_membership(COOKING_PROJECT_GID, "vq")],
        ),
    )

    with pytest.raises(BackendFailure) as caught:
        backend.create_bare_task(
            title="Created", project_gid=COOKING_PROJECT_GID, section_gid="vq"
        )

    _assert_uncertain_effect(caught, partial_application="task_created")
    assert caught.value.details["actual_task_gid"] == "wrong"


@pytest.mark.parametrize(
    ("method", "kwargs", "partial_application"),
    [
        pytest.param(
            "update_task_content",
            {"task_gid": "t", "title": "Changed", "notes": "Notes"},
            "content_update_requested",
            id="content",
        ),
        pytest.param(
            "update_task_completed",
            {"task_gid": "t", "completed": True},
            "completion_update_requested",
            id="completion",
        ),
    ],
)
def test_update_rejects_wrong_response_identity(
    monkeypatch, method, kwargs, partial_application
):
    backend = AsanaBackend(api_client=object())
    monkeypatch.setattr(backend, "call", lambda *args, **kwargs: {"gid": "wrong"})

    with pytest.raises(BackendFailure) as caught:
        getattr(backend, method)(**kwargs)

    _assert_uncertain_effect(caught, partial_application=partial_application)
    assert caught.value.details["expected_task_gid"] == "t"
    assert caught.value.details["actual_task_gid"] == "wrong"
