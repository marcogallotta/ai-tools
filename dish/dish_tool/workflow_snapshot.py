"""Read-side assembly of authoritative current-workflow facts."""
from __future__ import annotations

import sqlite3

from .database import pending_operation_steps, phase_candidate_actions
from .errors import DishRuleError
from .submission_authority import submission_authority_facts
from .task_store import read_complete_task
from .workflow_policy import WorkflowSnapshot


def build_workflow_snapshot(
    conn: sqlite3.Connection,
    backend,
    operation_id: str,
    op: sqlite3.Row,
    *,
    schema=None,
) -> tuple[WorkflowSnapshot, dict[str, object]]:
    from .constants import COOKING_PROJECT_GID
    from .models import SectionRegistry
    from .task_document import (
        DocumentParseError,
        parse_task_document,
        validate_task_document,
    )

    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    live_status = None
    validation_rules: list[str] = []
    try:
        document = parse_task_document(f"{live.title}\n{live.notes}")
        live_status = document.state.values["Status"]
        validation_rules = [
            finding.rule
            for finding in validate_task_document(
                document,
                expected_schema_version=op["schema_version"],
                schema=schema,
            ).findings
        ]
    except DocumentParseError:
        validation_rules = ["canonical_task_required"]

    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))
    cycle = conn.execute(
        """SELECT * FROM verification_cycles
           WHERE operation_id=?
           ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    cycle_reviewed = False
    if cycle is not None and cycle["completed_at"] is None:
        proof_ok = bool(str(cycle["run_id"] or "").strip())
        binding_ok = bool(cycle["reviewed_content_version_id"] and cycle["reviewed_identity"] and cycle["verifier_agent"] and proof_ok)
        actor = None
        if binding_ok:
            actor = conn.execute(
                """SELECT 1 FROM operation_actor_facts
                     WHERE task_gid=? AND operation_id=? AND role='verifier'
                       AND agent=? AND candidate_identity=?
                       AND COALESCE(run_id,'')=COALESCE(?, '')
                       AND COALESCE(independence_attestation,'')=COALESCE(?, '')
                     LIMIT 1""",
                (op["task_gid"], operation_id, cycle["verifier_agent"], cycle["reviewed_identity"], cycle["run_id"], cycle["independence_attestation"]),
            ).fetchone()
        cycle_reviewed = bool(binding_ok and actor is not None)

    dish_inspect_current = False
    if cycle_reviewed and cycle is not None:
        from .database import current_dish_inspect_fact
        dish_inspect_current = current_dish_inspect_fact(
            conn, cycle=cycle, section_gid=registry.verification_queue_gid
        ) is not None

    task_head = conn.execute(
        "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid=?",
        (op["task_gid"],),
    ).fetchone()
    required_identity = None if task_head is None else task_head["last_confirmed_identity"]
    required_section_gid = op["expected_section_gid"]
    required_cycle_exists = True
    signoff_bound = True
    held_baseline_matches = True
    preconstruction_hold = False
    research_hold = None
    movement_failure = None
    destination_repair_required = False
    phase = op["phase"]
    if phase == "await_verification":
        required_cycle_exists = bool(cycle is not None and cycle["completed_at"] is None)
        if cycle_reviewed:
            required_identity = cycle["reviewed_identity"]
        required_section_gid = registry.verification_queue_gid
    elif phase in {"await_submission", "ready_move_failed"}:
        try:
            submission_facts = submission_authority_facts(conn, operation_id)
        except DishRuleError:
            submission_facts = None
        signoff_bound = bool(
            submission_facts is not None
            and submission_facts.approved_identity
            and submission_facts.approved_cycle_id
            and op["signoff_completed_at"] is not None
        )
        required_identity = (
            None if submission_facts is None else submission_facts.effective_identity
        )
        if phase == "ready_move_failed" and submission_facts is not None:
            movement_failure = submission_facts.movement_failure
            destination_repair_required = submission_facts.destination_repair_required
        # Submission deliberately preserves a manual placement or recognises
        # an already-applied destination move. Exact approved-or-repaired
        # content remains mandatory.
        required_section_gid = None
    elif phase in {"held_evidence", "held_human"}:
        preconstruction = conn.execute(
            """SELECT intended_json FROM operation_steps
                 WHERE operation_id=? AND step_name='research_preconstruction_hold'
                   AND completed_at IS NOT NULL""",
            (operation_id,),
        ).fetchone()
        if (
            preconstruction is not None
            and op["operation_kind"] == "initial"
            and op["content_write_completed_at"] is None
        ):
            import json

            preconstruction_hold = True
            research_hold = json.loads(preconstruction["intended_json"])
            required_cycle_exists = True
            required_identity = op["expected_identity"]
            required_section_gid = op["expected_section_gid"]
            held_baseline_matches = bool(
                live.identity == required_identity
                and live.section_gid == required_section_gid
            )
        else:
            held = conn.execute(
                """SELECT * FROM verification_cycles
                     WHERE operation_id=? AND completed_at IS NOT NULL
                       AND (route IN ('evidence','human_review') OR outcome='verification-hold')
                     ORDER BY cycle_number DESC LIMIT 1""",
                (operation_id,),
            ).fetchone()
            required_cycle_exists = held is not None
            required_identity = None if held is None else held["hold_identity"]
            required_section_gid = None if held is None else held["hold_section_gid"]
            held_baseline_matches = bool(
                held is not None and held["hold_identity"] and held["hold_section_gid"]
                and live.identity == held["hold_identity"]
                and live.section_gid == held["hold_section_gid"]
            )
    destination_movement = None
    if op["movement_completed_at"] is not None and op["destination_movement_attempt_id"]:
        destination_movement = conn.execute(
            """SELECT confirmed_section_gid
                 FROM movement_attempts
                WHERE attempt_id=? AND operation_id=?
                  AND purpose='destination_submission'
                  AND outcome='confirmed'
                  AND confirmed_section_gid=intended_section_gid""",
            (op["destination_movement_attempt_id"], operation_id),
        ).fetchone()
    if destination_movement is not None:
        required_section_gid = destination_movement["confirmed_section_gid"]
    required_section = (
        None if required_section_gid is None
        else registry.by_gid.get(required_section_gid)
    )
    required_section_name = None if required_section is None else required_section.name
    identity_matches = required_identity is None or live.identity == required_identity
    placement_matches = required_section_gid is None or live.section_gid == required_section_gid
    unresolved_rows = conn.execute(
        """SELECT 'write:' || attempt_id AS item FROM write_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 'movement:' || attempt_id AS item FROM movement_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           ORDER BY item""",
        (operation_id, operation_id),
    ).fetchall()
    unresolved_attempts = tuple(row["item"] for row in unresolved_rows)
    unresolved_executions = tuple(
        row["execution_id"]
        for row in conn.execute(
            """SELECT execution_id FROM operation_executions
                 WHERE operation_id=? AND status='uncertain' AND resolved_at IS NULL
                 ORDER BY created_at, rowid""",
            (operation_id,),
        ).fetchall()
    )
    if unresolved_executions:
        unresolved_attempts += tuple(
            f"execution:{execution_id}" for execution_id in unresolved_executions
        )
    pending_steps = tuple(row["step_name"] for row in pending_operation_steps(conn, operation_id))
    migration_required = bool(op["migration_reconciliation_required"])
    from .semantic_proposals import semantic_proposal_action_facts

    proposal_facts = semantic_proposal_action_facts(
        conn,
        operation_id=operation_id,
        live_title=live.title,
        live_notes=live.notes,
        current_cycle_id=(
            None
            if cycle is None or cycle["completed_at"] is not None
            else str(cycle["cycle_id"])
        ),
        expected_schema_version=str(op["schema_version"]),
        schema=schema,
    )
    snapshot = WorkflowSnapshot(
        operation_status=op["status"],
        operation_phase=op["phase"],
        operation_kind=op["operation_kind"],
        persisted_actions=tuple(phase_candidate_actions(op)),
        live_status=live_status,
        live_section_gid=live.section_gid,
        verification_queue_gid=registry.verification_queue_gid,
        verifier_established=cycle_reviewed,
        latest_cycle_outcome=None if cycle is None else cycle["outcome"],
        latest_cycle_route=None if cycle is None else cycle["route"],
        validation_rules=tuple(validation_rules),
        pending_steps=pending_steps,
        unresolved_attempts=unresolved_attempts,
        migration_reconciliation_required=migration_required,
        identity_matches=identity_matches,
        placement_matches=placement_matches,
        required_cycle_exists=required_cycle_exists,
        signoff_bound=signoff_bound,
        held_baseline_matches=held_baseline_matches,
        preconstruction_hold=preconstruction_hold,
        destination_repair_required=destination_repair_required,
        dish_inspect_current=dish_inspect_current,
        semantic_proposal_status=(
            None if proposal_facts is None else str(proposal_facts["status"])
        ),
        semantic_proposal_actionable=bool(
            proposal_facts is not None and proposal_facts.get("actionable")
        ),
    )
    recovery_reasons: list[str] = []
    if op["status"] == "uncertain":
        recovery_reasons.append("operation_uncertain")
    if pending_steps:
        recovery_reasons.append("pending_workflow_steps")
    if unresolved_attempts:
        recovery_reasons.append("unresolved_external_attempts")
    if migration_required:
        recovery_reasons.append(str(op["migration_reconciliation_reason"] or "migration_reconciliation_required"))
    malformed_material_change_rules = sorted({
        rule
        for rule in validation_rules
        if rule in {"material-changes.format", "material-changes.field-count"}
    })
    if malformed_material_change_rules:
        recovery_reasons.append("historical_material_change_malformed")
    facts = {
        "status": op["status"],
        "phase": op["phase"],
        "live_status": live_status,
        "live_identity": live.identity,
        "required_identity": required_identity,
        "identity_matches": identity_matches,
        "live_section_gid": live.section_gid,
        "required_section_gid": required_section_gid,
        "required_section_name": required_section_name,
        "placement_matches": placement_matches,
        "validation_rules": validation_rules,
        "pending_steps": list(pending_steps),
        "unresolved_attempts": list(unresolved_attempts),
        "unresolved_execution_ids": list(unresolved_executions),
        "required_cycle_exists": required_cycle_exists,
        "cycle_reviewed": cycle_reviewed,
        "dish_inspect_current": dish_inspect_current,
        "signoff_bound": signoff_bound,
        "held_baseline_matches": held_baseline_matches,
        "preconstruction_hold": preconstruction_hold,
        "research_hold": research_hold,
        "movement_failure": movement_failure,
        "destination_repair_required": destination_repair_required,
        "recovery_required": bool(recovery_reasons),
        "recovery_reasons": recovery_reasons,
    }
    if proposal_facts is not None:
        facts["semantic_proposal"] = proposal_facts
        if proposal_facts["status"] == "pending":
            facts.update({
                "required_admin_action": "review-inspect",
                "continuation_surface": "private-admin",
                "connected_action_available": False,
            })
    if malformed_material_change_rules:
        facts.update({
            "required_admin_action": "manual-reconciliation",
            "resolver": (
                "Marco/admin must reconcile the malformed historical "
                "Material-change evidence and its durable exact-content binding"
            ),
            "continuation_surface": "manual-reconciliation",
            "connected_action_available": False,
            "admin_command": None,
            "directive": (
                "Tell the human: the durable historical Material-change evidence for this "
                "task is malformed and its exact-content binding cannot be trusted. This has "
                "no fixed recovery command; Marco must reconcile it directly. Do not start a "
                "new operation or retry until he confirms how to proceed."
            ),
            "historical_evidence": {
                "kind": "malformed-material-change",
                "validation_rules": malformed_material_change_rules,
                "automatic_rewrite": False,
                "required_scope": [
                    "live-task-evidence",
                    "durable-exact-content-binding",
                ],
            },
        })
    return snapshot, facts

