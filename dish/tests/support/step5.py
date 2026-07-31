
"""Shared helpers extracted from test_dish_tool_step5_commands.py."""


import json


from pathlib import Path

import pytest

from dish_tool.admin import DishAdminApplication

from dish_tool.commands import DishApplication

from dish_tool.constants import COOKING_PROJECT_GID

from dish_tool.database import confirm_task_content, initialize_database

from dish_tool.models import ResolvedRelease

from tests.support.asana_backend import StatefulAsanaBackend

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
Status: pending-verification
Status detail: None
Resume status: None
Verification protocol release: abc123
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

class Backend(StatefulAsanaBackend):
    def __init__(self, title="Bare", notes="", section="rq"):
        super().__init__(
            title=title,
            notes=notes,
            section=section,
            created_task_gid="new",
        )

def release(role=None, migrations=False):
    return ResolvedRelease(version="1.0.10", commit="", root=Path("."), protocols={} if role is None else {role:f"{role} protocol"}, manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}", migration_metadata={"m.json":{"migration_id":"m","from_schema_version":"1","to_schema_version":"2","protocol_version":"1.0.10","automatic":False,"description":"x","source_ids":["x"],"operations":[{"type":"canonical-parse-render","description":"test"}]}} if migrations else {}, requested_protocol_role=role)

def app(tmp_path, backend): return DishApplication(initialize_database(tmp_path/"d.db"), backend, release_loader=lambda role=None: release(role))
