"""Minimal sd_notify client for systemd Type=notify readiness reporting.

No `systemd` Python package dependency: this sends the same datagram protocol
directly over the `NOTIFY_SOCKET` the unit sets in the process environment.
Outside systemd (no `NOTIFY_SOCKET`, e.g. local dev or tests) this is a no-op.
"""

from __future__ import annotations

import os
import socket


def notify(state: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(address)
        sock.sendall(state.encode("utf-8"))
    except OSError:
        pass
    finally:
        sock.close()
