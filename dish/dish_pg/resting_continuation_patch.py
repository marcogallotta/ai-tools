"""Narrow runtime correction for resting canonical Dish continuation discovery."""
from __future__ import annotations

from typing import Any, Mapping

from .document_authority import CanonicalDocumentError, parse_canonical_document


def install(port_cls: type) -> None:
    """Make resting canonical Research/Verification states expose start(initial)."""

    original = port_cls._continuation_envelope
    if getattr(original, "_resting_canonical_continuation_patch", False):
        return

    def patched(
        self,
        *,
        call,
        task,
        operation,
        data: Mapping[str, Any],
    ):
        result_data, envelope = original(
            self,
            call=call,
            task=task,
            operation=operation,
            data=data,
        )
        if task is None or operation is not None or envelope.get("allowed_actions"):
            return result_data, envelope

        view = self.reads.task_view(task.task_id)
        if view.completed:
            return result_data, envelope
        try:
            document = parse_canonical_document(
                title=view.title,
                body=view.body,
            ).document
        except CanonicalDocumentError:
            return result_data, envelope
        if document.state.values["Status"] not in {
            "pending-research",
            "pending-verification",
        }:
            return result_data, envelope

        result_data = dict(result_data)
        result_data.setdefault("required_start_kind", "initial")
        result_data.setdefault(
            "agent_action",
            {
                "command": "start",
                "arguments": {
                    "dish_id": str(task.task_id),
                    "kind": "initial",
                },
            },
        )
        envelope = dict(envelope)
        envelope["allowed_actions"] = ["start"]
        return result_data, envelope

    patched._resting_canonical_continuation_patch = True
    port_cls._continuation_envelope = patched
