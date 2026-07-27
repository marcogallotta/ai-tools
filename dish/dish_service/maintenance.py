"""Reader/writer gate that makes database replacement exclusive."""
from __future__ import annotations

import contextlib
import threading


class MaintenanceGate:
    """Allow ordinary requests concurrently while giving restore exclusivity.

    Waiting restores block new requests so a steady request stream cannot starve
    database replacement.  The gate protects file replacement only; SQLite,
    workflow locks, and service leases remain the authorities for normal request
    concurrency.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_requests = 0
        self._restore_active = False
        self._restore_waiters = 0

    @contextlib.contextmanager
    def request(self):
        with self._condition:
            while self._restore_active or self._restore_waiters:
                self._condition.wait()
            self._active_requests += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_requests -= 1
                if self._active_requests == 0:
                    self._condition.notify_all()

    @contextlib.contextmanager
    def restore(self):
        with self._condition:
            self._restore_waiters += 1
            try:
                while self._restore_active or self._active_requests:
                    self._condition.wait()
                self._restore_active = True
            finally:
                self._restore_waiters -= 1
        try:
            yield
        finally:
            with self._condition:
                self._restore_active = False
                self._condition.notify_all()
