from __future__ import annotations
import uuid
import pytest
from dish_tool.admin import DishAdminApplication
from dish_service.admin_cli import build_parser
from tests.support.service_scenarios import RUN_ID, post as _post, running as _running
from tests.support.thread_teardown import join_thread, stop_server
from tests.support.submission import _signed

def _parse_generated_human_action(action):
    import re
    import shlex

    argv = shlex.split(action["shell_command"])
    assert argv[0] == "dish-admin"
    filled = [
        re.sub(r"<[^>]+>", "operator supplied reason", token)
        for token in argv[1:]
    ]
    return build_parser().parse_args(filled)
