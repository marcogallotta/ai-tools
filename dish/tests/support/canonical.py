
"""Shared helpers extracted from test_dish_tool_step2_canonical.py."""


import json

import sys

from pathlib import Path

import pytest

from dish_tool.migrations import migrate_task_document

from dish_tool.task_document import (
    FindingKind,
    PLANNING_FIELDS,
    parse_planning_brief,
    parse_task_document,
    validate_planning_brief,
    validate_task_document,
)

TASK = """[non-main] Test dish — crisp comparison side
A compact side dish for testing texture.
WHY COOK IT
Compare hydration routes.
## WHAT TO BUY
None - pantry snapshot lists required items in stock
## QUANTITIES
Portions: one sitting
100 g test ingredient
### Mise en place
Keep dry.
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
### Decisions
Human — Marco: Use the smaller batch, 2026-07-25, to isolate texture
### Research basis
Classification: Source-backed dish
source.example/test — Construction — hydration ratio — selected route is drier
### Material changes
2026-07-25 — ChatGPT — GPT-5 — tightened hydration — improve crispness — Large — pending-verification
Schema version: 2
"""
