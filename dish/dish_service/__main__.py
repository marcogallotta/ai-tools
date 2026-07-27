"""Run the laptop-hosted dish service."""
from __future__ import annotations

import argparse
import logging
import threading
from collections.abc import Sequence

from .application import DishService
from .config import ServiceConfig
from .http import build_action_server, build_private_server
from .process_lock import ServiceProcessLock
from .database_ownership import ServiceDatabaseOwnership


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="dish-service",
        description=(
            "Run the single-process Dish HTTP service. Runtime configuration is read "
            "from the service environment; see deploy/systemd/service.env.example."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = ServiceConfig.from_env()
    config.validate_runtime(require_action=True)
    lock_path = config.db_path.with_suffix(config.db_path.suffix + ".service.lock")
    with ServiceProcessLock(lock_path):
        ServiceDatabaseOwnership(config.db_path).mark()
        service = DishService(config)
        startup = service.startup_check()
        if not startup["ok"]:
            raise RuntimeError("dish service startup validation failed")
        if not startup["asana"]["ok"]:
            logging.getLogger("dish.service").warning(
                "Asana health check failed; mutation endpoints will remain fail-closed"
            )
        private_server = build_private_server(service)
        action_server = build_action_server(service)
        action_thread = threading.Thread(
            target=action_server.serve_forever,
            name="dish-action-http",
            daemon=True,
        )
        action_thread.start()
        try:
            private_server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            action_server.shutdown()
            private_server.server_close()
            action_server.server_close()
            action_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
