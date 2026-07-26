"""Exact backend trust boundary used by current workflow orchestration."""
from __future__ import annotations

from .task_store import move_exact, read_complete_task, write_exact_content


class ExactTaskGateway:
    def __init__(self, conn, backend) -> None:
        self.conn = conn
        self.backend = backend

    def read(self, **kwargs):
        return read_complete_task(self.backend, **kwargs)

    def write(self, **kwargs):
        return write_exact_content(self.conn, self.backend, **kwargs)

    def move(self, **kwargs):
        return move_exact(self.conn, self.backend, **kwargs)
