"""Recovery entry point: resume persisted plans through the shared executor."""
from __future__ import annotations


def recover_declared_plan(executor, *, operation_id: str, **kwargs):
    """Delegate recovery to the same idempotent executor used by normal paths."""
    return executor(operation_id=operation_id, **kwargs)
