"""Run both exact mixed pairs on separately installed real TEST resources."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import prod_release as release
from .release_hot import (
    Budget,
    OperationError,
    observe_component,
    pair_binding,
    run,
)

QUALIFICATION_CONFIG = Path.home() / ".config/dish-service/release-qualification.json"


def qualify_pair(target, candidate, retained):
    if not QUALIFICATION_CONFIG.exists():
        raise OperationError(
            "QUALIFICATION_NOT_INSTALLED",
            "real isolated pair qualification target missing; install the rehearsal qualification resources before prepare",
        )
    # Reuse exactly the TEST admission, with a separately installed config path.
    from . import release_hot

    old_path = release_hot.TEST_CONFIG
    try:
        release_hot.TEST_CONFIG = QUALIFICATION_CONFIG
        qualification = release_hot.target_config("test")
    finally:
        release_hot.TEST_CONFIG = old_path
    if (
        qualification.path("isolation_root") == target.path("isolation_root")
        if target.profile == "test"
        else False
    ):
        raise OperationError(
            "QUALIFICATION_ISOLATION",
            "qualification and operator rehearsal must be separate",
        )
    root = qualification.path("release_root")
    copies = []
    for source in (candidate, retained):
        copied = release.stage_release(
            target.path("repository"),
            source.source_commit,
            root,
            qualification.path("python"),
        )
        original = json.loads((source.root / release.MANIFEST_NAME).read_text())
        manifest = json.loads((copied.root / release.MANIFEST_NAME).read_text())
        if (
            original["source_tree_sha256"] != manifest["source_tree_sha256"]
            or original["installed_packages_sha256"]
            != manifest["installed_packages_sha256"]
        ):
            raise OperationError(
                "QUALIFICATION_IDENTITY",
                "isolated qualification source/dependencies differ from staged artifact",
            )
        copies.append(copied)
    evidence = []
    budget = Budget(60)
    pair_binding(target, candidate, retained)
    for backend, mcp in (copies, copies[::-1]):
        # Dedicated units use independent component pointers only inside this
        # qualification fixture. The operator controller has one paired pointer.
        release._set_pointer(qualification.path("current"), str(backend.root))
        release._set_pointer(
            qualification.path("isolation_root") / "mcp-current", str(mcp.root)
        )
        for component in ("backend", "mcp"):
            run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "restart",
                    qualification.values[f"{component}_unit"],
                ],
                budget,
            )
        observed = {}
        while True:
            observed = {
                "backend": observe_component(qualification, "backend", backend, budget),
                "mcp": observe_component(qualification, "mcp", mcp, budget),
            }
            if all(v["ok"] for v in observed.values()):
                break
            if budget.left() < 0.2:
                raise OperationError(
                    "PAIR_INCOMPATIBLE",
                    "real mixed-pair transport qualification failed",
                    observed=observed,
                )
            time.sleep(0.1)
        evidence.append(
            {
                "backend": backend.source_commit,
                "mcp": mcp.source_commit,
                "observed": observed,
            }
        )
    return {
        "boundary": "real user units + native PostgreSQL + authenticated FastMCP JWT TEST trust domain",
        "pairs": evidence,
        "observed_at": time.time(),
    }
