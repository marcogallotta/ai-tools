from __future__ import annotations

import builtins
import uuid

from dish_service import admin_cli
from dish_tool.admin_human import render_admin_result


def _envelope(command: str, *, data: dict, state: str = "ok") -> dict:
    return {
        "ok": True,
        "command": command,
        "code": "OK",
        "state": state,
        "retryable": False,
        "allowed_actions": [],
        "data": data,
        "errors": [],
    }


class ReviewApp:
    def __init__(self, item: dict):
        self.item = item
        self.calls: list[tuple[str, dict]] = []
        self.resolved = False

    def execute(self, command: str, **arguments):
        self.calls.append((command, dict(arguments)))
        if command == "review-queue":
            rows = [] if self.resolved else [self.item]
            return _envelope(
                command,
                data={"review_items": rows, "count": len(rows), "status_filter": arguments["status"]},
            )
        if command == "review-inspect":
            assert arguments["proposal_id"] == self.item["review_id"]
            return _envelope(
                command,
                state="pending",
                data={"review_item": self.item, "proposal": self.item},
            )
        if command in {"review-approve", "review-reject"}:
            assert arguments["proposal_id"] == self.item["review_id"]
            self.resolved = True
            return _envelope(
                command,
                data={"effect": "Recorded exactly once.", "review_id": self.item["review_id"]},
            )
        raise AssertionError(command)


def _answers(*values: str):
    iterator = iter(values)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(iterator)

    return answer, prompts


def test_interactive_semantic_review_inspects_uuid_before_approval(capsys):
    review_id = str(uuid.uuid4())
    item = {
        "item_type": "semantic_proposal",
        "review_id": review_id,
        "proposal_id": review_id,
        "status": "pending",
        "candidate_title": "Mapo tofu",
        "proposal_reason": "Correct the governed candidate.",
        "changes": [{"field": "Purpose", "before": "old", "after": "new"}],
        "linked_changes": [{"path": "Purpose", "before": "old", "after": "new"}],
    }
    app = ReviewApp(item)
    input_fn, prompts = _answers("1", "a")

    status = admin_cli._interactive_review_queue(
        app, status="active", arguments=(), input_fn=input_fn
    )

    assert status == 0
    assert [name for name, _ in app.calls] == [
        "review-queue",
        "review-inspect",
        "review-approve",
        "review-queue",
    ]
    assert app.calls[1][1] == {"proposal_id": review_id}
    assert app.calls[2][1]["proposal_id"] == review_id
    assert "Approved interactively after reviewing the exact linked change bundle" in app.calls[2][1]["reason"]
    assert any("Approve exact shown bundle" in prompt for prompt in prompts)
    output = capsys.readouterr().out
    assert "Governed changes" in output
    assert 'Purpose: "old" -> "new"' in output
    assert f"dish-admin review-inspect {review_id}" not in output


def test_interactive_human_review_records_the_actual_decision(capsys):
    review_id = str(uuid.uuid4())
    item = {
        "item_type": "human_review",
        "review_id": review_id,
        "proposal_id": review_id,
        "status": "pending",
        "candidate_title": "Pasta al limone",
        "proposal_reason": "Protein is below the main-dish threshold.",
        "review_summary": {
            "issue": "Protein is below the main-dish threshold.",
            "decision": "Choose whether to redesign the dish or change its role.",
            "simplest_next_step": "Record Marco's substantive choice, or dismiss an invalid escalation.",
        },
        "changes": [],
    }
    app = ReviewApp(item)
    decision = "Keep it as a main and pair it with the planned protein side."
    input_fn, prompts = _answers("1", "d", decision)

    status = admin_cli._interactive_review_queue(
        app, status="active", arguments=(), input_fn=input_fn
    )

    assert status == 0
    approve = next(arguments for command, arguments in app.calls if command == "review-approve")
    assert approve == {"proposal_id": review_id, "reason": decision}
    assert any("Record Marco decision" in prompt for prompt in prompts)
    assert any("decision and brief reasoning" in prompt for prompt in prompts)
    output = capsys.readouterr().out
    assert "Decision needed: Choose whether to redesign the dish or change its role." in output
    assert "Options: Record Marco's substantive choice" in output


def test_interactive_queue_renderer_shows_change_not_command_hopping():
    review_id = str(uuid.uuid4())
    result = _envelope(
        "review-queue",
        data={
            "review_items": [
                {
                    "item_type": "semantic_proposal",
                    "review_id": review_id,
                    "proposal_id": review_id,
                    "status": "pending",
                    "candidate_title": "Dish",
                    "proposal_reason": "Fix it.",
                    "changes": [{"field": "Purpose", "before": "old", "after": "new"}],
                }
            ]
        },
    )

    rendered = render_admin_result(result, profile="prod", interactive=True)

    assert 'Change: Purpose: "old" → "new"' in rendered
    assert "Select a number to inspect the exact decision or bundle." in rendered
    assert "dish-admin review-inspect" not in rendered
    assert "dish-admin review-reject" not in rendered


def test_review_queue_cli_enters_interactive_mode_only_in_tty(monkeypatch, capsys):
    review_id = str(uuid.uuid4())
    item = {
        "item_type": "semantic_proposal",
        "review_id": review_id,
        "proposal_id": review_id,
        "status": "pending",
        "candidate_title": "Dish",
        "proposal_reason": "Inspect me.",
        "changes": [{"field": "Purpose", "before": "old", "after": "new"}],
        "linked_changes": [],
    }
    app = ReviewApp(item)
    input_fn, _ = _answers("q")
    monkeypatch.setattr(builtins, "input", input_fn)
    monkeypatch.setattr(admin_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(admin_cli.sys.stdout, "isatty", lambda: True)

    assert admin_cli.main(["review-queue"], application=app) == 0

    assert [name for name, _ in app.calls] == ["review-queue"]
    assert "Select a number to inspect" in capsys.readouterr().out


def test_review_queue_non_interactive_flag_preserves_one_shot_listing(monkeypatch, capsys):
    review_id = str(uuid.uuid4())
    item = {
        "item_type": "semantic_proposal",
        "review_id": review_id,
        "proposal_id": review_id,
        "status": "pending",
        "candidate_title": "Dish",
        "proposal_reason": "Inspect me.",
        "changes": [],
    }
    app = ReviewApp(item)
    monkeypatch.setattr(admin_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(admin_cli.sys.stdout, "isatty", lambda: True)

    assert admin_cli.main(["review-queue", "--non-interactive"], application=app) == 0

    assert [name for name, _ in app.calls] == ["review-queue"]
    output = capsys.readouterr().out
    assert f"Inspect: dish-admin review-inspect {review_id}" in output


def test_interactive_review_inspect_suppresses_low_level_action_templates():
    review_id = str(uuid.uuid4())
    result = _envelope(
        "review-inspect",
        state="pending",
        data={
            "review_item": {
                "item_type": "human_review",
                "review_id": review_id,
                "status": "pending",
                "proposal_reason": "Choose the dish role.",
            },
            "human_actions": [
                {
                    "kind": "approve-human-review",
                    "summary": "Record Marco's decision.",
                    "shell_command": f"dish-admin review-approve {review_id} --reason '<decision>'",
                }
            ],
        },
    )

    rendered = render_admin_result(result, profile="prod", interactive=True)

    assert "Choose the dish role." in rendered
    assert "dish-admin review-approve" not in rendered
    assert "What you can do" not in rendered


class AttentionApp:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, command: str, **arguments):
        self.calls.append((command, dict(arguments)))
        if command == "attention":
            return _envelope(
                command,
                data={
                    "needs_you_count": 1,
                    "system_count": 0,
                    "category_counts": {"needs_marco": 1, "unsafe": 0, "system": 0},
                    "attention_items": [
                        {
                            "dish_id": "11111111-1111-5111-8111-111111111111",
                            "task_gid": "1217333270126271",
                            "task_title": "Mapo tofu",
                            "category": "needs_marco",
                            "needs_you": True,
                            "signals": [
                                {
                                    "kind": "human_decision",
                                    "category": "needs_marco",
                                    "summary": "Make the Human Review decision.",
                                    "shell_command": "dish-admin review-inspect review-1",
                                }
                            ],
                        }
                    ],
                },
            )
        if command == "inspect":
            return _envelope(
                command,
                data={
                    "dish_id": arguments["dish"],
                    "task_title": "Mapo tofu",
                    "status": "blocked",
                    "problem": "Dish is waiting for Marco's decision.",
                },
            )
        raise AssertionError(command)


def test_interactive_attention_drills_into_canonical_dish_without_command_hopping(capsys):
    app = AttentionApp()
    input_fn, prompts = _answers("1", "q")

    status = admin_cli._interactive_attention(app, arguments=(), input_fn=input_fn)

    assert status == 0
    assert app.calls == [
        ("attention", {}),
        ("inspect", {"dish": "11111111-1111-5111-8111-111111111111"}),
    ]
    output = capsys.readouterr().out
    assert "Select a Dish number to inspect its exact current state." in output
    assert "dish-admin review-inspect" not in output
    assert "Dish is waiting for Marco's decision." in output
    assert any("Dish number" in prompt for prompt in prompts)


def test_attention_cli_non_interactive_flag_preserves_one_shot_summary(monkeypatch, capsys):
    app = AttentionApp()
    monkeypatch.setattr(admin_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(admin_cli.sys.stdout, "isatty", lambda: True)

    assert admin_cli.main(["attention", "--non-interactive"], application=app) == 0

    assert app.calls == [("attention", {})]
    output = capsys.readouterr().out
    assert "dish-admin review-inspect review-1" in output
    assert "Select a Dish number" not in output
