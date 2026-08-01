from __future__ import annotations

from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_tool.database import initialize_database
from tests.planning_intent_support import confirmed_planning_start
from tests.support.request_restore import Backend


class SimulatedProcessDeath(BaseException):
    pass


ADMIN = ServicePrincipal(owner_id="admin", run_id="marco-run")
PLANNER = ServicePrincipal(owner_id="action", run_id="planner-run")
REQUEST_ID = "a0000000-0000-4000-8000-000000000001"
FRESH_ADMIN_ID = "a0000000-0000-4000-8000-000000000002"
FRESH_START_ID = "a0000000-0000-4000-8000-000000000003"
FRESH_START_CHALLENGE_ID = "a0000000-0000-4000-8000-000000000006"
ARGS = {"task_gid": "t", "reason": "repeat the cook"}


class CompletedBackend(Backend):
    def __init__(self):
        super().__init__()
        self.title = "Bare"
        self.notes = ""
        self.completed = True
        self.modified_at = "m0"
        self.reopens = 0
        self.fail_next_read = False

    def read_task(self, gid):
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("reread unavailable")
        task = super().read_task(gid)
        if gid == "other":
            task["completed"] = False
            task["modified_at"] = "other-m0"
        else:
            task["completed"] = self.completed
            task["modified_at"] = self.modified_at
        return task

    def update_task_completed(self, *, task_gid, completed):
        self.reopens += 1
        self.completed = completed
        self.modified_at = f"m{self.reopens}"


def restart(service, backend):
    return DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )


def rows(service):
    conn = initialize_database(service.config.db_path)
    try:
        attempt = conn.execute(
            "SELECT * FROM planning_reopen_attempts WHERE task_gid='t'"
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
        domain_audits = conn.execute(
            """SELECT COUNT(*) FROM audit_events
                 WHERE event_type='planning.task_reopened'
                   AND json_extract(details, '$.attempt_id')=?""",
            (None if attempt is None else attempt["attempt_id"],),
        ).fetchone()[0]
        invocation_audits = conn.execute(
            """SELECT COUNT(*) FROM audit_events
                 WHERE event_type='dish-admin.reopen-planning'
                   AND json_extract(details, '$.request_id')=?""",
            (REQUEST_ID,),
        ).fetchone()[0]
        return attempt, request, domain_audits, invocation_audits
    finally:
        conn.close()


def start(
    service,
    request_id=FRESH_START_ID,
    challenge_request_id=FRESH_START_CHALLENGE_ID,
):
    return confirmed_planning_start(
        service,
        {"agent": "gpt", "task_gid": "t", "kind": "planning"},
        principal=PLANNER,
        challenge_request_id=challenge_request_id,
        start_request_id=request_id,
    )


def fresh_reopen(service):
    return service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=FRESH_ADMIN_ID
    )


def exact_replay(service):
    return service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
    )


def assert_unresolved_blocked(service):
    start_result = start(service)
    assert start_result["code"] == "BACKEND_UNCERTAIN"
    assert start_result["errors"][0]["rule"] == "planning_reopen_reconciliation_required"
    assert start_result["data"]["original_request_id"] == REQUEST_ID
    fresh = fresh_reopen(service)
    assert fresh["code"] == "BACKEND_UNCERTAIN"
    assert fresh["errors"][0]["rule"] == "planning_reopen_reconciliation_required"
