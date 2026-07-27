"""Run the laptop-hosted dish service."""
from __future__ import annotations

import logging

from .application import DishService
from .config import ServiceConfig
from .http import build_server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service = DishService(ServiceConfig.from_env())
    server = build_server(service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
