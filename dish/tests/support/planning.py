
"""Shared helpers extracted from test_dish_tool_step6_prepare.py."""



from pathlib import Path

import pytest

from dish_tool.commands import DishApplication

from dish_tool.constants import COOKING_PROJECT_GID

from dish_tool.database import initialize_database

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

PLANNING = """### Planning brief
Dish candidate: Test dish
Purpose: Compare texture
Role: non-main — small side for comparison
Priors: None
Locks: Keep crisp
Exemptions: None
Research emphasis: Compare two hydration levels
Destination section: Sichuan — 12345
"""

Backend = StatefulAsanaBackend

def release(root, role=None):
    return ResolvedRelease(version="1.0.10", commit="", root=root, protocols={} if role is None else {role:f"{role} protocol"}, manifests={}, manifest_texts={}, schema_version="2", schema={}, schema_text="{}", migration_metadata={}, requested_protocol_role=role)

def app(tmp_path, backend):
    honest = tmp_path / "honest"; honest.mkdir(exist_ok=True)
    (honest / "dish-verification-protocol.md").write_text("verification protocol")
    return DishApplication(initialize_database(tmp_path/"d.db"), backend, release_loader=lambda role=None: release(honest, role))

def write(tmp_path, name, text):
    p=tmp_path/name; p.write_text(text); return str(p)
