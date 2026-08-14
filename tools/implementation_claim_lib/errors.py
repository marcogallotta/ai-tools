from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ClaimError(RuntimeError):
    code: str
    message: str
    status: int = 409
    current: dict | None = None
    # Set only when this exact response successfully minted fresh writer authority
    # but a subsequent fail-closed synchronization step failed. Never populate it
    # on status/conflict/ordinary authorization failures.
    writer_capability: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
