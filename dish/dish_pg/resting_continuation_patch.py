"""Narrow runtime corrections for connected PostgreSQL continuation discovery."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select

from . import stage3_models as wf
from .document_authority import CanonicalDocumentError, parse_canonical_document


def _verification_handoff(task_id: object) -> dict[str, Any]:
    return {
        "required": True,
        "requirement": "independent_verifier",
        "instruction": (
            "Hand this task to an independent caller. That caller must read the current "
            "task and follow its returned Verification continuation."
        ),
        "action_template": {
            "command": "read",
            "arguments": {"dish_id": str(task_id)},
            "required_caller_arguments": ["agent"],
        },
    }


def _verification_conflict(port, *, operation, call):
    if operation is None or operation.phase != "await_verification":
        return None
    return port.session.scalar(
        select(wf.OperationActorFact)
        .where(
            wf.OperationActorFact.operation_id == operation.operation_id,
            wf.OperationActorFact.actor_role != "verification",
            wf.OperationActorFact.run_id == call.run_id,
        )
        .limit(1)
    )


def _ineligible_metadata(task_id: object, actor_fact_id: object) -> dict[str, Any]:
    return {
        "verification_eligibility": {
            "eligible": False,
            "rule": "VERIFIER_NOT_INDEPENDENT",
            "conflicting_actor_fact_id": str(actor_fact_id),
        },
        "verification_handoff": _verification_handoff(task_id),
    }


def install(port_cls: type) -> None:
    """Install caller-aware Verification and resting-canonical continuations."""

    original_envelope = port_cls._continuation_envelope
    if getattr(original_envelope, "_resting_canonical_continuation_patch", False):
        return
    original_record_rule_failure = port_cls._record_rule_failure

    def patched_envelope(
        self,
        *,
        call,
        task,
        operation,
        data: Mapping[str, Any],
    ):
        result_data, envelope = original_envelope(
            self,
            call=call,
            task=task,
            operation=operation,
            data=data,
        )

        if (
            call.command_name == "read"
            and task is not None
            and operation is not None
            and envelope.get("allowed_actions") == ["start"]
            and result_data.get("required_start_kind") == "verification"
        ):
            conflicting = _verification_conflict(
                self,
                operation=operation,
                call=call,
            )
            if conflicting is not None:
                result_data = dict(result_data)
                result_data.update(_ineligible_metadata(task.task_id, conflicting.actor_fact_id))
                result_data.pop("required_start_kind", None)
                result_data.pop("agent_action", None)
                envelope = dict(envelope)
                envelope["allowed_actions"] = []
                return result_data, envelope

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

    def patched_record_rule_failure(
        self,
        call,
        exc,
        execution_id,
        task,
        operation,
    ):
        if (
            exc.code == "VERIFIER_NOT_INDEPENDENT"
            and task is not None
            and operation is not None
        ):
            conflicting_actor_fact_id = exc.data.get("conflicting_actor_fact_id")
            if conflicting_actor_fact_id is not None:
                exc.data.update(
                    _ineligible_metadata(task.task_id, conflicting_actor_fact_id)
                )
        return original_record_rule_failure(
            self,
            call,
            exc,
            execution_id,
            task,
            operation,
        )

    patched_envelope._resting_canonical_continuation_patch = True
    port_cls._continuation_envelope = patched_envelope
    port_cls._record_rule_failure = patched_record_rule_failure
