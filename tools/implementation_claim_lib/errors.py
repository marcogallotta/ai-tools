from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ClaimError(RuntimeError):
    code: str
    message: str
    status: int = 409
    current: dict | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
