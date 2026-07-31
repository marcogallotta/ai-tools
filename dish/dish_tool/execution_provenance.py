"""In-process provenance for durable operation-execution evidence."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class OperationExecutionProvenance:
    execution_id: str
    operation_id: str
    connection_id: int


_CURRENT_EXECUTION: ContextVar[OperationExecutionProvenance | None] = ContextVar(
    "dish_operation_execution_provenance", default=None
)


@contextmanager
def operation_execution_provenance(
    conn: sqlite3.Connection, *, execution_id: str, operation_id: str
) -> Iterator[None]:
    """Bind audit writes on one connection to one exact operation execution."""

    provenance = OperationExecutionProvenance(
        execution_id=str(execution_id),
        operation_id=str(operation_id),
        connection_id=id(conn),
    )
    token = _CURRENT_EXECUTION.set(provenance)
    try:
        yield
    finally:
        _CURRENT_EXECUTION.reset(token)


def current_operation_execution_id(
    conn: sqlite3.Connection, *, operation_id: str | None
) -> str | None:
    """Return the current execution only for its connection and operation."""

    if operation_id is None:
        return None
    provenance = _CURRENT_EXECUTION.get()
    if (
        provenance is None
        or provenance.connection_id != id(conn)
        or provenance.operation_id != str(operation_id)
    ):
        return None
    return provenance.execution_id
