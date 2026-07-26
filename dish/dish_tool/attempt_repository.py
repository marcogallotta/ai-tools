"""Persistence boundary for external write and movement attempts."""
from __future__ import annotations

from .database import (
    finalize_confirmed_movement_attempt,
    finalize_confirmed_write_attempt,
)
from .recovery import begin_movement_attempt, begin_write_attempt


class AttemptRepository:
    def __init__(self, conn) -> None:
        self.conn = conn

    def begin_write(self, **kwargs):
        return begin_write_attempt(self.conn, **kwargs)

    def confirm_write(self, **kwargs):
        return finalize_confirmed_write_attempt(self.conn, **kwargs)

    def begin_movement(self, **kwargs):
        return begin_movement_attempt(self.conn, **kwargs)

    def confirm_movement(self, **kwargs):
        return finalize_confirmed_movement_attempt(self.conn, **kwargs)
