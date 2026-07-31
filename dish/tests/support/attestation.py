from __future__ import annotations


"""Shared helpers extracted from test_dish_tool_easy_backlog.py."""


import json

import pytest
from tests.support.readiness import _approve_and_submit
from tests.support.verification import TASK, make_app



ATTESTATION = "independent verifier run"
