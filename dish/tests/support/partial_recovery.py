from pathlib import Path

from dish_service.application import DishService
from dish_service.config import ServiceConfig
from dish_tool.constants import COOKING_PROJECT_GID

from tests.support.asana_backend import StatefulAsanaBackend
from tests.support.planning import Backend, app, release, write
from tests.support.service_foundation import _release_loader as release_loader

TASK = """[non-main] Test dish — crisp comparison side
A compact side dish for testing texture.
WHY COOK IT
Compare hydration routes.
## WHAT TO BUY
None - pantry snapshot lists required items in stock
## QUANTITIES
Portions: one sitting
100 g test ingredient
## HOW TO COOK IT
1. Cook it.
## WHAT SUCCESS LOOKS LIKE
Crisp and aromatic.
---
## PROCESS RECORD
Status: pending-research
Status detail: Continue research
Resume status: None
Verification protocol release: None
Researched by: ChatGPT — GPT-5, 2026-07-25
Verified by: None
Self-verified: ChatGPT — GPT-5, 2026-07-25
### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
### Research basis
Classification: Source-backed dish
source.example/test — Construction — hydration ratio — selected route is drier
Schema version: 2
"""


class ServiceBackend(StatefulAsanaBackend):
    def __init__(self):
        lines = TASK.splitlines()
        super().__init__(
            title=lines[0],
            notes="\n".join(lines[1:]) + "\n",
            task_gid="t",
            created_task_gid="1000000000000001",
        )


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def service(tmp_path):
    backend = ServiceBackend()
    honest = tmp_path / "honest"
    honest.mkdir(exist_ok=True)
    service = DishService(
        ServiceConfig(
            db_path=tmp_path / "shared.db",
            honest_root=honest,
            backup_dir=tmp_path / "backups",
            port=0,
            agent_token="agent-secret",
            admin_token="admin-secret",
            action_token="action-secret",
        ),
        backend_factory=lambda: backend,
        release_loader=release_loader(honest),
    )
    return service, backend


def started_application(tmp_path):
    lines = TASK.splitlines()
    backend = Backend(lines[0], "\n".join(lines[1:]) + "\n")
    application = app(tmp_path, backend)
    started = application.execute(
        "start",
        agent="gpt",
        task_gid="t",
        kind="initial",
        change_level=None,
        change_reason=None,
    )
    return application, backend, started["submission_id"]


def fault_at_step(monkeypatch, step_name):
    import dish_tool.step6 as step6

    real_complete = step6.complete_operation_step

    def fail_once(conn, operation_id, current_step):
        if current_step == step_name:
            raise RuntimeError(f"fault after {step_name}")
        return real_complete(conn, operation_id, current_step)

    monkeypatch.setattr(step6, "complete_operation_step", fail_once)
